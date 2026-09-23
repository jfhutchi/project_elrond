# Counterfactual operator report (#57)

Approved scope: connect durable refusals and outcomes to the existing gate-value
calculation through a read-only JSON CLI. No evaluator or statistical-method changes.

`quantbot counterfactual-report [--policy-identity ID] [--by-regime]` opens the
configured existing store without composing runtime. Default policy is the existing
`HypotheticalExecutionPolicy().identity`, printed in the response. Historical policies
are selected explicitly and available identities are listed for discoverability.

Group by strategy, configuration, gate, gate version and parameter hash. Count distinct
candidate IDs, preserving observation IDs for traceability. Conflicting answers for
the same candidate in a group fail rather than selecting an answer. Outcomes from
different execution policies never mix. Missing rows mean NO_RECORDED_OUTCOME:
maturity is unknown, not a fabricated pending horizon. Explicit UNAVAILABLE,
UNEVALUABLE and EVALUATED states and reasons remain visible.

The optional regime view groups stored method version, benchmark, source and three
axes. It retains snapshot IDs and unavailable reasons. Missing bindings/rows have
explicit unavailable context; future-dated bindings are invalid. No reconstruction
from current prices or definitions. Report groups are exploratory, not registered
confirmatory tests. Existing effective-size suppression stays authoritative; missing
dependence-group measurements are disclosed and never replaced by regime labels.

No writes, migrations, network calls, broker authority or policy changes. Temporary
database tests exercise real CLI startup, byte/logical history preservation, missing
stores, version separation, candidate deduplication, regime gaps and suppression.
Uncertainty intervals, tail/catalyst/liquidity analysis and deployment evidence remain
outside this bounded slice, so #57 stays open.
