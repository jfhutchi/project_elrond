# Elrond Learner implementation plan

**Goal:** Implement issue #242's supervised learning loop while preserving Elrond's evidence
and trading authority boundaries. The operator explicitly authorized autonomous implementation.

## Design

Use a focused `quantbot.learner` package and the existing SQLite/Alembic store. Daily features
have separate availability timestamps and use only data available at the prediction cutoff.
The initial target is five exchange sessions of security return minus SPY return. Calendar
gaps must never shorten that horizon. Features and outcomes have distinct types.

TRAIN maps to existing `DataRole.DISCOVERY`; validation maps to `VALIDATION`. Protected holdout
maps to `PROTECTED_EVALUATION`, and is inaccessible to ordinary learner commands. Persist
chronological boundaries and purge observations whose target crosses the next boundary.
Existing protected-window and observation-identity accounting remain authoritative.

Fit a no-skill baseline and a regularized linear model before considering nonlinear extension.
Every training run creates a new immutable identity with data, feature, code, environment and
artifact hashes. Durable predictions distinguish historical replay from prospective local
receipts; unverified local sources cannot create genuine shadow observations. A caller-supplied
backdated clock must not manufacture forward evidence.
Outcome resolution appends evidence to a previously committed prediction. Evaluation reports
cross-sectional rank IC and spreads with explicit insufficient-evidence handling.

Historical experiments establish software operation and exploratory predictive measurements.
They establish neither protected confirmation nor profitability. No order or promotion path is
introduced until independent evidence justifies one.

## Ordered checkpoints

- [x] 1–2: Local-declared dataset, feature/label separation, calendar horizon, provenance,
  chronological purge and protected-window refusal; focused adversarial tests and commit.
- [x] 3–4: Trainable mean/ridge/quadratic ridge, immutable artifacts and lineage; migration round trip,
  deterministic fitting/reload tests and commit.
- [x] 5–6: Durable predictions and delayed outcome resolution, replay/local-receipt separation;
  timestamp and identity tests and commit.
- [x] 7: IC, quantile spreads, sample/coverage diagnostics, no training fallback; known-result
  fixtures and commit.
- [ ] 8 onward: Walk-forward and qualified challengers. Descriptive matched-validation comparison
  and immutable parent/successor lineage are available; they do not qualify a challenger.
- [x] CLI workflow, synthetic local-data demonstration and status documentation.
- [ ] Publish prepared #242 implementation note (external posting requires approval after automatic
  approval review rejected the attempted architecture comment).
- [x] Focused tests, required pytest, lint, both mypy targets and native Windows local CI.
  Final source `09ba047`: 91 learner tests passed; full CI 3,048 passed, 47 skipped.
- [ ] Linux runtime CI: requested, but NOT RUN because Docker is unavailable. Both platform
  mypy targets passed on Windows; this does not establish Linux runtime parity.

## Initial evidence

- Read #242 (no comments), CLAUDE.md and repository architecture/status/refutation guidance.
- Started from upstream `92c49d4` on `codex/elrond-learner`; original checkout was clean.
- Baseline focused data-snapshot, forecasting and storage tests: 72 passed, 2 platform skips.
- Existing historical US-equity windows have accumulated search burden. Renaming a dataset
  or fitting a model cannot make them untouched confirmation.

## Completed scope and remaining evidence

The core local research loop is committed and demonstrated on synthetic data. All sources remain
LOCAL_DECLARED; replay never becomes genuine forward evidence. No market edge, protected result,
walk-forward qualification, portfolio economics or PAPER performance is established.

See [the prepared #242 update](elrond-learner-issue-242-update.md) for exact commits, test results,
the unfavorable ridge-versus-mean validation result and remaining production ingestion work.
