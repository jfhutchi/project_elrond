# M4 shadow numerical learning implementation

Accepted base `c8fbf4d3b1256856aeadf45066ac0371c0f0cd9a`; accepted M3 source
`1730b658f1a67132525abdfaab40547f0a3e624c`. Isolated worktree
`C:/Users/leepa/.codex/worktrees/paper-core-m0/project_elrond`, branch
`codex/paper-core-m0`. The focused commit SHA is returned in the final handoff.
The exact tested files/byte hashes and affected command are in
`docs/evidence/paper-core/m4/validation.json`.

## Implementation

- Shadow ridge reuses `learner.estimators` with the ten names/math from
  `learner.five_minute`. The target is actual owned `net_pnl / entry_notional`,
  converted once to bps. Predictions clip to ±100 bps; alpha 1, seed 0, 30-second
  child/parent program deadlines, one worker/fit per completed eligible session,
  last 60 sessions, 30 training across 5 sessions, 10 validation across 5 later
  sessions, 30-minute embargo and 5% MSE improvement are fixed. Training and
  validation feature/holding windows do not intersect. Zero baseline error
  cannot be counted as improvement. Worst validation-session error cannot worsen.
- Eligibility and canonical protection are evaluated from indexed metadata
  before any outcome/feature materialization. The registry covers known aliases,
  protected and forward-PAPER windows; SQL extracts only holdout metadata from
  existing learner declarations. Unavailable, unknown-cost or incompatible rows
  are excluded before hydration. The selected documents are then validated and
  persisted as immutable exact-revision joins. No old market-label target or
  scheduler is reused. No code-SHA training filter is introduced.
- The canonical `learner:paper-core-<run_id>` search row and immutable reservation
  are committed before export/fit. `HypothesisRegistry.learner_trials_spent`
  sees every failure and historical burden. Canonical alias-aware contamination
  now prevents inspected learner date ranges from being relabeled untouched
  protected evaluation, including failed-but-charged attempts. There is no
  private budget or inherited experiment scheduler's six-call cap.
- Parent admission locking and child process-lifetime locking reuse
  `SingleWriterLock`. Each run has a distinct immutable scratch input/result and
  permission digest. The child locks before checking permission and before math.
  Recovery must acquire both locks, cancel a delayed orphan's permission and
  append an interrupted receipt. A currently fitting orphan refuses recovery;
  its own 30-second watchdog exits124 even after parent loss. Acquisition of the
  released OS lock supplies fresh lifetime evidence. Old results never bind to a
  new consideration; permanent session charges are never reset. Outcomes and
  guardian remain independent.
- Models, input exports and receipts are immutable. The candidate's
  `learning_receipt_id` denotes the deterministic consideration `run_id`, which
  maps through `core_learning_considerations` to the immutable receipt ID. This
  avoids a circular artifact/receipt hash and supplies exact M5 provenance.
  Failed/rejected receipts retain parent=result; successful candidates remain
  `AWAITING_ACTIVATION`. Every activation time is null. There is no active pointer,
  activation API or operational model authority.
- Historical COLLECT payload/ID and its nonpredictive meaning are preserved.
  Ten-feature COLLECT fixtures use their exact declared feature contract during
  claim replay. A PREDICT artifact receives `SHADOW_MODEL_NOT_ACTIVATED` and cannot
  cause a POST. The explicit −20bps incumbent is synthetic fixture authority,
  not a fitted or approved operational incumbent.
- Additive schema 33 stores learning documents/reservations and its explicit
  revision-map registration. Frozen v30/v31/v32 migrations/factories are
  untouched. The populated accepted-v32 fixture was captured before changes;
  original timing, observations, claimed decisions, protection, costed outcomes
  and COLLECT IDs/payload bytes survive migration and decision replay. Exports
  now report the actual schema version.

## Integrated evidence

`joined-update.json.gz` contains all 40 complete original joined traces and the
receipt/artifacts: 30 actual M2 filled/protected/reconciled costed synthetic trades
over5 sessions plus 10 over5 later sessions. Reported synthetic fee activities
provide explicit known zero; balanced executed net outcomes are±20bps. Causal
bars go through the existing five-minute feature builder. No targets are invented
by inserting a training table. Ridge parameters change; positive-feature prediction
is 19.86754966887444 bps, exceeding the 10 bps reserve for later M5 use.

Candidate MSE is0.017543090215340906bps², against constant 400 and incumbent 800;
worst-session metrics match those values. The parent constant−20bps and resulting
coefficients/scaler are exported. Root independently recomputed the scaler,
ridge coefficients with separate Gaussian elimination, predictions, split,
canonical references, cost/fill arithmetic and session metrics from the frozen
trace. This fixture demonstrates numerical behavior, not profitability.

`joined-no-change.json.gz` is a complete two-trade insufficient-evidence witness
with parent=result, no trial, an explicit absent-validation reason and null
activation. `fit-failed.json.gz` proves failure after permanent charging and exact
idempotent replay. `cost-correction.json.gz` retains the original candidate's old
input IDs, records the corrected current revision and 40 trade samples, and
truthfully refuses another fit for the already-spent session.

`process-recovery.json.gz` distinguishes an explicitly terminated disposable
orphan, an automatic child watchdog expiry, and a delayed pre-lock orphan after
actual controller process loss during `consider_learning`. Recovery preserves
the charge, rejects the old result and keeps the parent. A later actual pair of
owned outcomes in the next session permits another bounded fit; canonical trials
become 2, with 42 outcomes. This is actual local fixture-process evidence; no
operational service is changed.

`summary.json`, `exports.json`, `logs.json` and `validation.json` provide a small
index, original/raw/compressed byte hashes, source pins and exact commands.
Decode any artifact with Python `gzip.decompress(Path(path).read_bytes())`.
Compressed large logs preserve original bytes; original raw logs remain at
their recorded temporary paths. No reference arrays or joins are omitted.

## Verification

The root-selected affected command in `validation.json`, run through isolated
credential-stripped offline `check.py`, passed **508, skipped 26, failed 0,
590.12 seconds, exit0**. Original raw log:
`C:/Users/leepa/AppData/Local/Temp/paper-core-check-_pvuhiat/result.txt`.
`green-affected.txt.gz` is a byte-identical lossless copy. All fourteen frozen
source/test/fixture hashes were unchanged at packaging and independently
verified by root. The 26 skips are disposable PostgreSQL infrastructure cases.
The final gate includes the automatic orphan-watchdog expiry and actual
`consider_learning` controller loss, as well as the retained M0–M3, canonical
learner/registry/storage and numerical estimator/feature checks. No source/test
changes occurred during or after this final run; no additional tests are needed.

Six exact offline credential-stripped CI commands each exit 0 on frozen source:

```powershell
uv run --frozen ruff check src tests
uv run --frozen mypy --platform linux src
uv run --frozen mypy --platform win32 src
uv run --frozen ruff check scripts/ci_checks.py scripts/local_ci.py scripts/ci_process.py
uv run --frozen mypy --platform linux scripts/ci_checks.py scripts/local_ci.py scripts/ci_process.py
uv run --frozen mypy --platform win32 scripts/ci_checks.py scripts/local_ci.py scripts/ci_process.py
```

Source type checks cover 340 files; helpers cover 3 each platform. No mypy tests.
`ci-results.json` records commands/log hashes. Source/test diff whitespace is clean.

TDD evidence: initial missing-interface/envelope red 14 failures; the behavioral
shadow refusal red shows an otherwise BUY decision with no refusal; the strict
zero-error improvement gate has its own expected red. These are retained in
`red-interfaces`, `red-shadow-refusal` and `red-zero-baseline`. The first real
numerical fit passed 14 tests in232.58 s. A later moving-source focused run passed 30
in 285.34 s, explicitly intermediate because child-watchdog/controller amendments
followed. Metadata boundaries passed 27 in 7.03 s; populated-v32 replay passed 1
in 2.68 s; 60-session/intersection selection passed 1 in 2.41 s. Those counts are separate
runs, not a final full-suite claim.

Intermediate migration-reference/naming, feature/claim fixture, novelty-fixture
and schema-head collection failures are preserved and named. The first stable
affected invocation stopped at collection with `KeyError: 33`, before executing
tests; the one-line explicit revision-map registration repairs that defect.
Pre-registration CI logs are separately prefixed `initial-`. Final frozen logs
and source hashes govern the handoff; no repository-wide suite was duplicated.

## Self-review, migration and limits

Owned paths are the fourteen source/test/fixture entries in `validation.json`,
the M4 evidence directory and this report. Root plan/spec/ledger/reviews remain
root-owned and unstaged. Shared edits are limited to typed contracts, COLLECT
claim replay/PREDICT refusal, export version, canonical consumption and additive
storage registration. `learning.py` contains the focused selection/fit/receipt
and process protocol for this milestone; independent review should examine the
lock/cancellation/receipt boundaries as well as numerical correctness.

The learner currently supports the same explicit SYNTHETIC_TEST fixture lane as
the M1–M3 core. Other-lane metadata is refused before access; real-data/broker
adapters and operational commissioning remain M5/M6 work. SQLite fixture
migration, payload replay and existing backend-generation checks do not prove
actual PostgreSQL execution. A program watchdog/file interface is not a new OS
sandbox or hard scheduling guarantee. No market selection-bias or lasting
profitability claim follows from validation on this synthetic traded cohort.

Migration was exercised on populated copies only. Ordinary opening never
migrates. v33 refuses evidence-destroying downgrade; rollback requires compatible
code or a forward repair, keeping permanent trials and all old/candidate/failed
records. No operational database/store was read or migrated. No credentials,
real broker/network calls, OS/security/service/task changes, stop clearing,
forced operational trades, new worktree/chat/delegation, activation, runtime
deployment, push or merge occurred. Only explicitly-created disposable test
processes were fault-injected. Independent review and owner acceptance follow
the focused commit; M5/M6 and whole-project completion remain unclaimed.
