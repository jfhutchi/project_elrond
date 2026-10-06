# PAPER execution attribution and bounded challenger research

`quantbot.learner.trade_learning.read_trade_episodes` reads a snapshot-consistent,
read-only operational store. It binds a five-minute forecast to its exact durable
deadline handoff and entry intent, then joins broker fills through client order
identities. Only exits explicitly owned by that handoff are attributed. Existing
holdings and exits belonging to other protection owners are not guessed.

A completed execution outcome requires matching quantities, terminal orders,
prior owned inventory, and close confirmation after the last fill. Partial fills,
missing confirmations, unknown protective-exit ownership and ambiguous ordering
remain explicit non-profit states. Fees reduce realized P&L. Future events cannot
enter an earlier as-of read.

`journal_trade_episodes` appends immutable content-addressed revisions to a
separate journal. Re-reading unchanged fills is idempotent. Execution diagnostics
are not unbiased supervised market-return labels: only executed trades are
observed, and account deposits/resets are not trade profit.

`quantbot.learner.adaptive_research` fits exactly two prespecified families,
ridge and quadratic ridge, with alpha 1 and seed 0. Callers must provide canonical
holdout guards and permanent pre-engine trial accounting. Both charges precede
either engine call. An existing experiment directory, including one from a failed
run, cannot be repeated. There is no final refit, order authority or model promotion.

Historical training is split by market target time, independently of archive
retrieval time. All training outcomes must have been available before the forward
cutoff. Guards cover the feature lookback through market target time; retrieval
availability is separately constrained. Source ranges must also be guarded before
opening archived evidence.

Chronological validation diagnostics use an unchanged 10-basis-point round-trip
cost assumption and nonoverlapping 30-minute windows. Reference marks are not
executable fills; additional slippage is not established. Seen validation is
explicitly provisional. Five fresh completed post-fit sessions are a proposed
evaluation window, not an earned deployment qualification or a promise of profit.

The active strategy, its binding, universe, risk controls and scheduled services
are unchanged. This source slice does not automatically consume the journal in a
production training task or replace the deployed model. Those integration steps
require a separately reviewed rollout and, for model replacement, explicit approval.

Validation: deterministic forecast/intent/fill/exit/fee/journal tests; corrupted
lineage, late evidence, partial fills, equal-time ambiguity, permanent failed-fit
charges, chronological separation, and delayed historical retrieval regressions.
The final learner unit group passed 391 tests; Ruff and strict mypy passed for
both new modules. Synthetic test outcomes are not prospective learner evidence.
# Scheduled attribution and raw-return shadow evaluation

`quantbot.learner.tracking_launch` runs independently of entry and protection.
Its immutable config binds the existing observer deployment envelope, complete
executable Python tree hash, release revision, selected strategy and exactly two
frozen raw-return artifacts. It reads operational SQLite in query-only mode and
writes only its isolated journals and local guard database. No broker credentials,
order constructor, fitting or promotion API is loaded by the launcher.

Market training labels include forecasts rejected by execution gates. Logical
feature/target identities count once; alternate resolution receipts remain
provenance records, and conflicting economic labels fail closed. Broker-fill
episodes remain a separate selection-biased execution diagnostic with actual
fees, partial-fill uncertainty and exact ownership. They do not replace market
labels. No automatic refitting or active-model replacement is enabled.

Raw-return shadows share the existing ten-feature DIA contract and thirty-minute
horizon. Each forecast checks model creation, producer receipt, actual recording
time, existing clock verification and unchanged ten-basis-point floor. Late
cohorts are skipped, not backfilled. The observational schedule may collect only
a subset of producer cohorts; missing outcomes remain unknown. Shadow costs are
five-basis-point-per-side reference-mark assumptions, not calibrated broker fills.
True broker outcomes require eligible orders and complete owned fill receipts.
