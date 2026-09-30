# Learner recovery handoff — September 21, 2026

## Preserved work and branch

Resumed existing codex/elrond-learner at b3bd609 in
C:/Users/leepa/OneDrive/Documents/GitHub/project_elrond. It was clean and contained the existing
learner dataset/model/forecast/resolution implementation plus two operational fixes. Fetch
confirmed origin/main=92c49d4 and branch divergence 12 ahead / 0 behind before this session.
No merge, rebase, reset, forced checkout or deployment occurred. Implementation commit: 8a78dc3.

## Measured runtime and recovery

Omen is Leannes-Laptop, native Windows. Runtime checkout is C:/Users/leepa/elrond-omen-runtime,
clean detached a49203d; task pins match. Source operational store remains schema 28 / revision
0028 at D:/Elrond/state/quantbot-paper-fresh-20260908.db, lineage
cd2a5911-853a-484e-bbc8-d0c27b5d27aa. Environment file remains external at
C:/Users/leepa/.config/quantbot/quantbot.env; no credentials copied into development artifacts.

Used existing deployment-status inventory first. WAL required a consistent snapshot, so the
supported SQLite backup produced D:/Elrond/backups/learner-recovery-20260921T140713Z.db.
Integrity check: ok. Seven runs, six qualification rows, 356 equity snapshots, one strategy
identity survived. Original backup unchanged; a separate research copy alone migrated to 29.

Fresh deployed doctor verified PAPER account identity, PAPER broker endpoint, risk health,
connected stream and reconciled state. Entry readiness remains false: MARKET_DATA_STALE.
Fresh reconcile returned zero differences and no unresolved intents. No stale intents replayed.
Protection at 14:00 UTC: HEALTHY, nine positions evaluated, zero unprotected, zero exits required.
Latest ingestion observed September 18 close. Last successful trading cycle September 16
20:05 UTC. No fills after that cycle in the 14:07 backup, and no deployed forecast tables.
No missing predictions, offline qualifications, execution results or forward evidence fabricated.
Kill switch was already clear; no control was cleared or weakened. Entry staleness refusal stays.

Console/daemon/protection are Windows task-owned process trees in session 2 under leepa.
Six application tasks use Interactive logon, with logon/timed triggers. They are independent of
Codex's process tree but depend on Windows sign-in. Closing Codex/logging out/rebooting was not
experimentally tested. Model server is session 0; task principal erlond, process-owner read
unavailable. Both configured local models (qwen3:4b-instruct and qwen2.5:3b) returned HTTP 200 to
small requests at 127.0.0.1:11434. No paid API fallback or Claude dependency was used. Research
status has four UNDERPOWERED tasks, no actionable tasks. No scheduler, task principal, power,
security setting or runtime code changed. The Pi and other hosts were not contacted.

## Implemented extension

Guarded stored ETF replay and frozen source checks; next-open/fifth-close, full-liquidation
portfolio simulation against matched all-universe and SPY baselines at 5 bps per side. Complete
validation coverage required. Immutable reports retain exact execution inputs and hashes.
Independent review's incomplete-cohort and changed-source findings were fixed; the resolver also
reuses verified inputs so it cannot race a second raw price read. No promotion path introduced.

The existing dataset, estimators, immutable persistence, maturity/idempotency rules and model
comparison survived. This extension makes #242's dataset → fit → durable historical forecasts
→ matured outcomes → predictive/portfolio evaluation → immutable successor → matched comparison
work on the stored market panel. Protected qualification, authenticated prospective SHADOW,
paper execution integration and demonstrated profitability are not completed by this slice.

## Measured historical result

One fixed experiment; 22 ETFs, 56 decision cohorts, 1,232 resolved forecasts per model and
12 nonoverlapping five-session portfolio periods. Models are mean, ridge (alpha 1, seed 0),
and an identically configured ridge successor. No parameter tuning followed these results.

| Portfolio | Net return | Excess vs matched SPY | Max close-based drawdown | Cost / initial capital | Traded notional / initial capital |
| --- | ---: | ---: | ---: | ---: | ---: |
| Mean / equal-weight universe | -0.1304% | +5.8854 pp | 6.3100% | 1.2308% | 24.6156x |
| Ridge top quintile | -3.4606% | +2.5552 pp | 9.2885% | 1.2058% | 24.1158x |
| Ridge successor top quintile | -3.4606% | +2.5552 pp | 9.2885% | 1.2058% | 24.1158x |
| Matched-window SPY | -6.0158% | 0 | 11.0188% | 1.1979% | 23.9585x |

All portfolios have mean daily gross exposure 1.0 over this sampled span. Traded notional is
both-side turnover under full liquidation, normalized by initial capital. The mean model ties
every security and therefore selects the entire universe. Its rank IC is undefined, not zero.
Ridge and successor mean rank IC are -0.1642938; validation MSE 0.0007336668 versus mean
0.0005918158. Ridge loses to the simple baseline by 3.3302 percentage points after costs.
Neither its positive relative return against falling SPY nor passing software tests establishes
an investment edge. These are exploratory historical replay results, not authenticated
out-of-sample evidence, forward shadow forecasts, paper execution returns or proven profitability.

The research store retains all seven prior search rows unchanged and exactly three additional
fit attempts (10 search rows total), one dataset, three models, 3,696 predictions and 3,696
outcomes. Every prediction is HISTORICAL_REPLAY. Four report hashes and database integrity
passed verification. The successor has a distinct ID, version 2 and the ridge parent ID, with
the same artifact hash and 1,232 matched outcomes; comparison explicitly returns promotion NONE.

| Model | ID | Clean training commit |
| --- | --- | --- |
| Mean | dabbbc27-5d1e-4264-9052-3517fe57792e | 8a78dc3 |
| Ridge | 1aeaa765-4bb9-4881-900c-5ff25707448e | 419f545 |
| Ridge successor | 32e17efe-0e93-4832-9040-9cb4b14aceed | 419f545 |

Dataset ID: `16781deed8a12d69651c4b2d45462c79cd1ed8698416971170c20f75b42e5350`.
Source ID: `stored-replay:077da40d6323b6cef394b86a6aed73aa3a8d3b55af59cab87d2293280e42f0f4`.
All economic reports record clean evaluation commit 419f545 and preserve execution inputs.

## Boundaries and next task

Current bars lack historical receipt and membership vintages; LOCAL_DECLARED historical replay
cannot establish authentic point-in-time or forward SHADOW evidence. Existing adjusted IEX prices,
fixed ETF selection, correlated exposures and limited periods constrain interpretation. Protected
challenger qualification and promotion remain outstanding. Issue 242 remains open.

Highest-value next task: collect immutable provider receipt and universe-membership provenance
through the existing ingestion path, allowing real prospective shadow forecasts with a decision
cutoff after data publication. Preserve all accumulated research attempts and evaluation boundaries.

## Reproduction and retained artifacts

Development implementation: 8a78dc3; range-check performance correction: 419f545.
The explicit research copy is .local-ci/learner-market-20260921/research.db. Its spec.json pins
22 ETFs plus SPY benchmark, broker-sourced 320-session calendar, TRAIN 2025-09-22..2025-12-31,
VALIDATION 2026-01-02..2026-03-31, and manifest-only HOLDOUT 2026-04-01..2026-09-30.
No prices on/after the holdout start are read by the learner, although the source backup contains
later operational market data. Its protection/search registry was retained. The snapshot is not
claimed fresh confirmation. A copied registry is not a global authority or automatic trial sync.

The first run was deliberately interrupted after the broad panel exposed redundant per-symbol
protection decoding. One fit and 572 durable forecasts were preserved. The normal idempotent
predict command resumes them; no refit, record deletion, account reset or source price changes
were used. The ignored resume.py records the exact recovery commands. The normal reproducible
entry point for a new explicitly authorized run is:

```powershell
.\.venv\Scripts\python.exe scripts/learner_market_replay.py --database RESEARCH_COPY.db --spec spec.json --output-dir NEW_RUN_DIRECTORY
```

Do not rerun research simply to get better performance. Keep this store's additional attempts
with its artifacts, and consolidate accounting before any future protected qualification.
The full per-command ledger is run/steps.jsonl; economic outputs pin model/data/code identities,
prices, calendars, hashes and cost assumptions. No databases or sensitive runtime logs are in Git.

Artifact root (local only):
`C:/Users/leepa/OneDrive/Documents/GitHub/project_elrond/.local-ci/learner-market-20260921`.
Its `verification.json` records independently checked counts, clean training commits and returns;
`run/summary.json` and the three `*-economics.json` files are immutable hashed results.

Implementation files: `src/quantbot/learner/store.py`, `src/quantbot/learner/economics.py`,
`src/quantbot/learner/cli.py`, `src/quantbot/learner/service.py`,
`src/quantbot/research/registry.py`, and `scripts/learner_market_replay.py`.
Regression files: `tests/unit/learner/test_store.py`, `tests/unit/learner/test_economics.py`,
`tests/integration/test_learner_store_workflow.py`. CLI details and input schema are in
`docs/elrond-learner.md`; implementation decisions are in
`docs/superpowers/plans/2026-09-21-learner-market-replay.md`.

## Verification scope

Full native Windows CI was launched from clean implementation commit 8a78dc3, in the existing
local-CI isolated checkout. The later 419f545 correction only consolidates overlapping protection
checks; 105 learner/service/burden tests and full Linux/win32 mypy targets passed after it.
Do not describe the earlier full-suite run as having executed the later commit. All 244
source/new-helper files passed both manual type-check targets; lint covers source/tests and the
new runner. Linux runtime execution: NOT RUN, Docker absent; no WSL/Docker setup attempted.

Windows full-suite result: 3,062 passed, 47 skipped, one existing Starlette deprecation warning,
778.99 seconds. Lock consistency, frozen install, lint, both mypy platform targets and CI-helper
checks passed. Combined local-CI exit 1 records the unavailable Linux job. Evidence lives at
`.local-ci/20260921T142624Z-eb056d74/{summary.json,windows.log,linux.log}`. Reproduction:

```powershell
.\.venv\Scripts\python.exe -I -S -B scripts/local_ci.py --platform all
```

The final documentation commit changes no executable code. Corrected Windows CLI examples use
`.\.venv\Scripts\quantbot.exe learner`; the package has no `python -m quantbot` entry point.
Runtime remained clean at a49203d after research completion. Before the final documentation
commit, development was 14 ahead / 0 behind fetched origin/main; the handoff commit adds one.

The independent reviewer reproduced the missing-cohort and source-race failures in synthetic
databases. The source-race regression now changes prices after verification and confirms the
original verified values are resolved. Synthetic tests are control evidence, never returns evidence.
