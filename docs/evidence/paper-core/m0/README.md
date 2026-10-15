# M0 implementation evidence

Base: `b812c64270f190b0518322abb08b9a17dfa3f1c0`. Isolated branch:
`codex/paper-core-m0`. No production code deployed or operational state changed.

The exporter reads explicit SQLite paths using `mode=ro`, `query_only=ON` and a
read transaction per store. It reads existing WAL data; it does not construct
`Database`, a broker client, or an experiment writer. Transactions are separate:
this is not an atomic cross-store snapshot or a new broker reconciliation.

## Reproduction

Use the frozen project environment (`uv sync --frozen --offline --group dev`).
`manifest.json` contains permitted absolute paths and an account hash, never
credentials. Host metadata is separately timestamped; its capture time is not
broker freshness. `deployment.json` remains a historical claim.

```powershell
python -m quantbot.paper_core.inventory --config docs/evidence/paper-core/m0/manifest.json --as-of 2026-10-14T10:38:28.907218+00:00 --lot aaed112ab59c1b415ec5c3910b240d9ff62ff23e03d920e80d6775478c4010d6
python -m pytest tests/integration/paper_core/test_inventory.py -q
```

`capture-host.ps1` emits allowlisted local task/process metadata to stdout; save
to a new file and set `runtime_metadata` explicitly. Never export raw command lines.
CLI emits JSON to stdout only. Do not redirect it over an input file. A later
run may have additional records: the cutoff excludes future timestamps but
cannot reconstruct overwritten mutable rows or prove when a legacy fill first
became available. Repeated inspection of unchanged stores/cutoff is deterministic.
Source identity and a digest of selected evidence accompany every store.

## Observed integration result

- 18 authoritative operational fills; all 12 learner-store fills are duplicate
  copies; zero conflicting or copy-only fills. Repeated export adds no samples.
- All three requested lots have recorded observation, entry intent, entry fill,
  protection, exit fill, flat evidence and feedback links.
- XLI lot `aaed112…` joins observation `753684…`, intent `learner-entry-79f37…`,
  broker entry `1d08c75c-4dba-497c-9357-a0169eedafb2`, quantity `0.014766`, entry
  `169.404`, exit `169.116`, and gross `-0.004252608`. These match the audited
  evidence. The other two historical lots have gross `0.002019028` and `0.001563558`.
- Every lot remains `COSTS_PENDING`; verified net is null. Historical zero fees
  are retained as stored claims, never upgraded to contractual/reported zero.
  Feedback has `predictor_fit=false`; no linked parameter-update claim is made.
- Host metadata captured 19 Elrond tasks and 12 quantbot Python process records.
  `TASK_DEPLOYMENT_CONFLICT:console` identifies the stale deployment-file pin
  `4e643ae…` versus task pin `b9e5e9f…`. Process paths and old DB observations
  do not prove unique writer ownership or current broker readiness.
- Recorded entry reason is `MARKET_CLOSED / NORMAL_WAIT` at
  `2026-10-14T10:36:40.439666Z`, age 108.467552 seconds at export. The source receipt
  is separately digested and attributed to release `b496109…`. The older halted
  cycle retains `KILL_SWITCH_ENGAGED` at its original timestamp; it is not current
  stop authority. Standalone lot bundles include configured and observed account
  hashes. No accountless or mismatched lot row may supply evidence.

`inventory.json` retains per-store records. `lots.json` is the joined history,
including repeated protection/reconciliation observations with original digests.
The booleans under `stages` mean recorded evidence is present, not that this
read-only exporter has certified its correctness or granted order authority.
Source policies retain historical code hashes; current runtime releases are
reported separately and must not be relabeled as the release of an old trade.

## Verification record

- Initial 6 integration tests failed on missing exporter (expected red).
- 6 passed after implementation; added task-pin/future-occurrence regressions
  failed before their fixes. Review regressions also reproduced before fixes.
  Current focused set: 15 passed; independent rerun: 15 passed (5.07 s).
- Existing runtime-inventory baseline: 25 passed. First attempt had 2 fixture
  errors because the credential-stripped runner omitted Git from PATH; rerun
  with explicit Git/system paths passed (31 including the initial 6 new tests).
- Ruff CI targets (src, tests, and CI helpers) passed; type targets for Linux and Windows passed
  (322 production/helper files each). Frozen lock check and offline sync passed.
- An exploratory `mypy src tests` invocation is not the repository CI target:
  it reported 3,005 errors in 211 files. This result is not represented as green.
- Independent technical review accepted M0 after fixing two Important and one
  Minor finding; see [review.md](review.md). Integration owner accepts M0 after
  the per-case regression resolution below.
- The first full-suite attempt failed collection in ten sandbox-related modules
  because the stripped environment lacked a home directory. The rerun uses a
  temporary USERPROFILE/APPDATA/LOCALAPPDATA and no inherited credentials.
- Full offline run: **4,253 passed, 48 failed, 59 skipped, 5 setup errors** in
  1,077.66 s. This ran the initial nine M0 tests; the final fifteen were verified
  separately. The runner omitted required Windows tool paths/environment values
  and blocked fixture HTTP sockets. No production code was changed to fix it.
- All 53 failed/errored cases, final M0 cases, and formerly PATH-skipped native
  test files were rerun after correcting the runner: **101 passed, 14 failed,
  1 skipped** in 115.25 s. The fourteen remaining cases selected restricted
  Windows PowerShell. Read-only inspection found the installed PowerShell 7 uses
  its existing RemoteSigned policy; adding that executable directory to the
  child PATH resolved **all 14 on rerun** (8.99 s). No policy/security setting
  was changed. The remaining skip needs managed Python discovery in the
  disposable home. Other original skips require absent optional integrations.
- Every initial failure/error has a passing rerun. These are separate runs,
  **not a claimed single clean full-suite run or sum of unique tests**. Full
  initial failure names and rerun summaries are retained alongside this record.
  Loopback connections are permitted only to ports bound by fixture code in the
  test process; external connects remain blocked. This is a test harness guard,
  not an OS sandbox claim.

No migration or rollback action is required. Withdrawal of this exporter has no
runtime effect. M1 is now the next ready milestone. M0 does not change the
running PAPER kill-switch behavior; scoped recovery is implemented in M3.
