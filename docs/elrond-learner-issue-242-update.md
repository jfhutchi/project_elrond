# #242 implementation update — prepared for GitHub

This is a local, reviewable issue update. It has **not** been posted to GitHub. Automatic approval
review rejected the earlier architecture-comment action as a durable external disclosure without
explicit approval of its payload and destination. No issue comment, push or pull request was made.

## Architecture verdict: APPROVE WITH CHANGES

Start with a supervised five-session excess-return learner using the existing research store,
DataRole boundaries, migration authority and cumulative search accounting. Use the installed
Python stack: mean, ridge and a fixed quadratic-ridge expansion require no heavyweight framework.
Preserve all existing promotion, execution and risk authority.

Source provenance is the main constraint. A caller-declared calendar, static universe and local
price file cannot authenticate point-in-time market evidence. This implementation therefore marks
all sources `LOCAL_DECLARED`, separates replay from prospective local receipts, and refuses to
create genuine `SHADOW` evidence. A protected-safe production loader is still required.

## Implemented and tested

- Twelve point-in-time feature calculations with explicit availability, a five-session benchmark-
  relative label, exact calendar targets, chronological TRAIN/VALIDATION and boundary purging.
  Protected holdout remains a manifest with no labels loaded by the dataset builder.
- Trainable mean, ridge and quadratic-ridge estimators with training-only scaling, immutable
  artifacts, exact eligible observation IDs, validation diagnostics and parent/successor lineage.
- Schema 29 in the existing SQLite/Alembic store: datasets, model versions, durable predictions,
  resolved outcomes and pinned evaluation records. Reads validate stored hashes and lineage;
  storage records receipt time independently and refuses same-session outcome resolution.
- Local prospective issuance has a five-minute deadline after the feature cutoff. Historical
  replay is explicitly labeled. Current input sources cannot mint genuine shadow evidence.
- Rank IC, period diagnostics, coverage, realized ranks, quantile returns/spreads, directional hit
  rate and selection turnover. Incomplete coverage suppresses IC/spreads. Overlapping labels do
  not receive an IID t-statistic; the descriptive non-overlapping statistic requires 20 periods.
- Every attempted learner fit is durably counted before fitting in existing global search burden.
- Protected-window checks use the registry's canonical reservation lifecycle and known aliases.
  Resolution rechecks current protection before using target prices, including reservations added
  after prediction issuance. Learner holdout manifests cannot be recycled after time elapses.
- Normal `quantbot learner dataset/train/predict/resolve/evaluate/compare/status` commands bypass
  trading-runtime construction. Comparison is descriptive and has no promotion authority.

## Demonstrated result

The committed CLI demonstration uses **generated synthetic data**, not historical market data.
It trains three model families, creates a fourth immutable ridge successor, and resolves/evaluates
175 validation predictions for each ridge version. All 350 predictions are `HISTORICAL_REPLAY`.
There are zero prospective, shadow or PAPER observations and zero holdout input prices.

| Synthetic estimator | TRAIN MSE | VALIDATION MSE |
| --- | ---: | ---: |
| Mean baseline | 0.000168018873273 | 0.000219983314387 |
| Ridge | 0.000000575125126 | 0.000962562935709 |
| Quadratic ridge | 0.000000510974671 | 0.000186535175165 |

Ridge's validation error is worse than the mean baseline's. The demonstration preserves this
negative comparison; no data or parameter changes were made to force a win. The synthetic ranking
diagnostic is not market evidence. Repeating the ridge fit produces the same fitted parameters
under a new identity, proving reproducibility and lineage rather than challenger improvement.

Retained local artifacts: `.local-ci/learner-demo-bba060a/report.json`, `input_bundle.json` and
`learner.db`. The demo was run from clean implementation commit `bba060a`.
Repository reload verified each artifact hash and `code_dirty=false`: ridge has 12 learned
nonzero coefficients, quadratic ridge has 24, and the repeated ridge artifact hash is identical.

## Verification

- Focused learner unit/integration tests on final source: **91 passed**.
- Migration-authority tests: **19 passed**.
- Registry, learner search burden and service protection tests: **74 passed**.
- Ruff and both Linux/Win32 mypy targets passed, including the learner demonstration script.
- Final native Windows CI on source commit `09ba04782aae84bc8de5d96b0aabc6976d85cb54`:
  **PASS**, including lock verification, frozen installation, lint, both platform type targets,
  the full pytest suite, and clean-tree checks. Pytest: **3,048 passed, 47 skipped**, one upstream
  Starlette deprecation warning, in 701 seconds.
- Both CI platforms were requested. Linux: **NOT RUN — DOCKER_NOT_FOUND**. The combined command
  correctly exited 1 because Linux could not run. No Docker/WSL installation was attempted.
  Optional WSL, PostgreSQL, model-server, deployed-checkout and platform/bootstrap checks remain
  skips, not verified capabilities. This is local CI, not hosted approval or Linux runtime parity.

The final CI invocation used the existing interpreter:

```powershell
.\.venv\Scripts\python.exe -I -S -B scripts/local_ci.py --platform all
```

Logs and the platform result are retained under
`.local-ci/20260915T030106Z-73137fde/` (`windows.log`, `linux.log`, `summary.json`).
Earlier runs were stopped when review produced additional fixes; they are not counted as passes.

Implementation checkpoints on `codex/elrond-learner`:
`5ef8e07`, `26fc73f`, `9c53801`, `7a9c74f`, `04617b9`, `bba060a`, `556b040`,
`316244c`, `09ba047`.

## NOT YET PROVEN

No verified market edge, untouched holdout confirmation, qualified challenger, after-cost portfolio
result, genuine forward shadow/PAPER result or positive expectancy. No operational database was
migrated and no broker action was taken.

Next work is authoritative point-in-time universe/provider ingestion with protection checked
before raw reads, authoritative outcome sourcing, then walk-forward qualification. Portfolio
economics, baseline portfolio comparisons, promotion and automated retraining remain outstanding.
Issue #242 remains open; this is the local research loop, not completion of its scientific goal.
