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

## Repair round 1 and approved daily-policy amendment

This section supersedes the original fixed-split policy description above. It
implements the October 14 user amendment and the two independent temporal
REWORK findings against source `30abc2804e6e32c1d2e411554a7cb74b042d5316`.
The original bundle, source pins, raw/log hashes and immutable payloads remain
unchanged. New evidence is exclusively in `docs/evidence/paper-core/m4/fix1/`.
Root owns the authoritative plan/spec and review; none is part of this commit.

Execution now has an independent clock argument, defaulting to the store clock.
Synthetic tests declare `SYNTHETIC_TEST:m4-execution-v1`; input cutoff is solely
an evidence boundary. Selection checks input and execution availability and
uses fresh execution time for canonical reservation lifecycle. The immediate
materialization fence samples execution time again. New receipts record actual
start, optional observed fit start, completion/availability, and declared start
and completion domains. Cross-domain recovery retains original start evidence,
records the new completion domain and grants no comparable availability. Old
reservations lacking genuine start/domain evidence start a new recovery event;
old completed receipts are replayed without edits and have unknown availability.

Canonical search rows remain immutable permanent trial-charge events: actor
`paper-core-shadow-trial-charge`, explicit `PERMANENT_TRIAL_CHARGE` detail,
start=end actual charge time, and null fit completion in that event. The linked
receipt supplies actual fit completion. No frozen migration/schema factory or
historical search/receipt/model payload is rewritten. The populated original
v33 fixture proves exact old IDs, raw payloads and pointer/accounting replay;
the accepted v32 fixture remains unchanged.

New `LearningPolicy()` defaults to `daily-all-history-walk-forward-v2` and no
history cap. A supplied immutable `LearningCalendar` wraps the retained XNYS
session sequence with explicit synthetic provenance and content digest. Ten
completed exchange sessions (including quiet sessions) are the warmup. Missing
calendar, insufficient samples, incompatibility, unavailable evidence and
unchanged revisions have explicit NO_CHANGE. Calendar completion determines
attempt day. Original serialized max60 policies still decode with their exact
IDs; new legacy fits refuse `LEGACY_POLICY_REPLAY_ONLY` unless explicitly opted
into a synthetic compatibility fixture. That opt-in is a Python test seam, not
runtime configuration or commissioning authority. Original receipt replay stays
available without it. No model activation interface has been added.

All current eligible history enters the gated final fit. Five expanding folds
use only prior outcomes/revisions available by each validation session open,
with 30-minute feature/holding-window embargo. Historical revision metadata is
extracted in SQL before any outcome/feature materialization; late corrections
cannot replace the versions a past fold could know. Validation labels expose
scoring availability. Both current and as-of selections exclude unresolved
lot-scoped CORE_EVIDENCE_CONFLICT incidents from metadata, even when an old
COMPLETE pointer persists; the writer fence rechecks incident scope before
hydration without globally disabling learning from unrelated healthy lots.

The pure `learning_procedure.py` runs all weighted preprocessing, five fits,
historical/constant scoring, procedure gates, successful final refit and exact
reproduction inside the existing one-child 30-second bound. Parent admission,
child lifetime locks, watchdog, cancellation/generation fencing and permanent
charges are retained. Raw weights are2 for episodes in the latest10 completed
calendar sessions at EACH fit cutoff and1 for older episodes, normalized to
mean1. Optional weighted retained-estimator mode stores normalized weights,
uses weighted mean/scaler/intercept/ridge objective/training MSE and a distinct
algorithm version. Default unweighted arithmetic/serialization/IDs are unchanged.

Comparisons use exact contemporaneous decision models. Only a canonical
reconstruction whose ID equals a historical COLLECT decision supplies zero.
PREDICT requires its actual saved estimator and original qualifying modern fit
receipt, plus the decision's acknowledged receipt (which may be a later
NO_CHANGE retaining that same model), each available before the decision in a
comparable execution domain. Exact model, original/ack receipt IDs, availability
and immutable payloads are exported. Missing, postdated and wrong-model
provenance refuses explicitly. The -20bps fixture parent remains separately
labeled lineage/benchmark; it is never substituted for actual COLLECT0 history.

The final model is labeled `PASSED_FIXED_PROCEDURE_GATES`; its exported final
input IDs/weights/parameters include development-validation labels. Its explicit
`FINAL_REFIT_HAS_NO_INDEPENDENT_HOLDOUT` reason prevents describing procedure
scores as an independent final-model holdout. All activation times remain null.
These are synthetic engineering demonstrations, conditional on the collection
policy, and make no profitability or unbiased opportunity-return claim.

Focused evidence: six genuine temporal reds preceded six greens; ten extended
temporal cases passed; weighted mode had six expected reds followed by45
weighted/default estimator passes; daily missing-calendar/all-history had two
expected reds; quarantined stale-COMPLETE pointers had two expected reds.
Fixture/collection mistakes and subsequent greens are preserved separately.
Combined64 focused cases passed in36.48s, followed by17 child-only daily cases
in19.59s after exact comparator artifacts/payloads moved into the child. A
separately labeled narrow process replay used a scratch copy of the original
synthetic before-fit DB: alive orphan refused, actual child watchdog exited124,
then fresh released lifetime locks allowed recovery without resetting charges
(1passed40.85s). This reuse is solely a fault reproduction; final integrated
acceptance generates fresh actual trades through production code.

The final32-target affected gate uses the original19 targets plus the owner's
12 retained estimator/calendar targets and the new weighted-estimator test.
Twenty raw source/test/fixture pins are frozen in fix1/validation.json. The six
exact credential-stripped, offline CI checks passed: `uv run --frozen ruff check
src tests`; source mypy with platforms linux and win32 (341 source files); ruff
on scripts/ci_checks.py, scripts/local_ci.py, scripts/ci_process.py; and mypy on
those three helper files with both platforms. Tests are not mypy targets. No
entire-repository pytest repeat is performed.

Final affected gate: **691 passed,26 skipped in641.72s**, exit0. All26 skips
are the unchanged disposable PostgreSQL parity cases requiring an unavailable
admin URL; no credentials or live/operating database were read. Raw log:
`C:/Users/leepa/AppData/Local/Temp/paper-core-check-cplnujtq/result.txt`.
The complete command, raw log hash, lossless gzip log,13 complete JSON exports,
17 preserved focused/final logs, six CI logs and20 source pins are indexed in
`docs/evidence/paper-core/m4/fix1/{validation,exports,logs,ci-results}.json`.

Fresh main procedure: fold training counts30/32/34/36/38,10 scored outcomes,
final all40 inputs; candidate procedure MSE0.013965653516800786 against400 for
both historical COLLECT0 and fold-training-only constant, worst-session
MSE0.017543090215340906 against400. Final all40 positive-feature prediction
**19.900497512437546bps**. The following actual fee correction refits all40
current revisions, using original revisions in every past fold and preserving
those scores; final positive prediction19.895105553314774bps. The following quiet
completed session records NO_CHANGE without another trial. The process recovery
witness adds two actual production-generated trades and fits all42 inputs on a
later completed session, retaining the original failed session charge. The
separate66-session/41-input numerical fixture proves old-data influence and1:2
raw weights; its extra shifted/inverted synthetic episode is a pure numerical
boundary fixture, not an additional certified trade. Root independently
recomputed fold/final weighted scalers and ridge through a separate Gaussian
solve, scores, cost arithmetic, canonical joins and exact comparator provenance.

No tests were repeated after this successful stable gate. All frozen source
pins and CI hashes remained unchanged during lossless evidence packaging.
Independent re-review is the remaining owner acceptance step. No activation,
M5/runtime work, operational database or process/service/security changes,
broker-network calls, deployment, push or merge are part of this source commit.
