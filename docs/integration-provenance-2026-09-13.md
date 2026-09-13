# Branch provenance, verified suites and merge order — 2026-09-13

Re-verified from `D:\GitHub\project_elrond` on `LEANNES-LAPTOP` rather than carried over from
[the 2026-09-12 record](integration-provenance-2026-09-12.md), which this corrects in three places.
Commands ran between `2026-09-12T23:54:26Z` and `23:55:18Z` unless stated. Nothing was force-pushed,
reset, rebased or deleted; `main` was not changed locally or remotely.

## 1. Why local `main` is `c817094` while `origin/main` is `7dc26dd`

```
$ git fetch --prune origin                                   # 23:54:35Z
$ git merge-base main origin/main          -> c817094a4bfd8aa68d1e41dd0cc0d8d8fe9eafc3
$ git log --oneline origin/main..main      -> (nothing)
$ git branch -vv                           -> main c817094 [origin/main: behind 59]
```

Stale, not divergent: local `main` is a strict ancestor, 0 ahead and 59 behind. The 59 are PRs
#220–#228 merged 2026-09-05..09-09 and never pulled into this clone. Left untouched.

## 2. `codex/omen-paper-runtime`: it *was* on the remote, and was deleted after its PR merged

The 2026-09-12 record said the branch "is not on the remote" and exists only in a second clone. The
first half is true now; the history is more specific:

```
$ git ls-remote --heads origin | grep omen-paper-runtime     # 23:54:59Z -> (no output)
$ gh pr view 228 --json headRefName,headRefOid,mergeCommit,mergedAt,state
  headRefName codex/omen-paper-runtime   headRefOid 302bf8ed51e261d3b963c4ee187d6557e4903cef
  state MERGED   mergedAt 2026-09-09T02:09:49Z   mergeCommit 7dc26dd99b1d9b48ced879f5699afa696f9cef2c
```

It was the head of PR #228 at `302bf8e`, merged into `main` as `7dc26dd`, and the remote branch was
deleted afterwards. The deployed runtime is a worktree of a second clone under OneDrive whose local
branch of that name kept moving:

```
$ git -C C:/Users/leepa/elrond-omen-runtime rev-parse --git-dir HEAD --abbrev-ref HEAD   # 23:55:08Z
  C:/Users/leepa/OneDrive/Documents/GitHub/project_elrond/.git/worktrees/elrond-omen-runtime
  5025b21586223d05dfeb4c37c2a514338a25a711
  codex/omen-paper-runtime
$ git -C C:/Users/leepa/OneDrive/Documents/GitHub/project_elrond branch -vv
  codex/omen-paper-runtime 5025b21 [origin/codex/omen-paper-runtime: ahead 4]
```

"ahead 4" is measured against that clone's never-pruned tracking ref for the deleted branch
(`302bf8e`). A PR cannot be opened from it, and none is needed: the four commits are on the remote
under other names (below).

## 3. Ancestry

```
$ git merge-base --is-ancestor 302bf8e origin/main              -> yes (via the #228 merge)
$ git log --oneline origin/main..5025b21
  5025b21 fix(reconciliation): distinguish explained equity marks from ledger drift
  c45be7e docs(ops): record opening fills and verified protection recovery
  477f885 docs(ops): record reboot recovery and market-hours deployment
  f125bd5 fix(ops): monitor market open after overnight daemon starts
$ git merge-base --is-ancestor 5025b21 origin/codex/equity-reconciliation      -> yes
$ git merge-base --is-ancestor 5025b21 origin/codex/market-hours-monitoring    -> no (ends at c45be7e)
```

| branch | head | base | ahead / behind `origin/main` |
|---|---|---|---|
| `codex/market-hours-monitoring` | `c45be7e` | `302bf8e` | 3 / 1 |
| `codex/equity-reconciliation` | `9b44885` | `302bf8e` | 5 / 1 (contains the 3 above) |
| `claude/status-file-deadlock` | `4f4c28b` | `7dc26dd` | 4 / 0 |
| `claude/windows-sandbox-backend` | `88d2f6a` | `7dc26dd` | 5 / 0 |
| `claude/research-proposer` | `ac546f3` | `7dc26dd` | 18 / 0 (contains the sandbox branch) |
| `claude/trial-runtime-integration` | `adccca0` | `7dc26dd` | 5 / 0 — tree identical to `5025b21` |

"Behind 1" for the two `codex/` branches is only the #228 merge commit itself; their base `302bf8e`
is already in `main`.

## 4. Does a remote branch already carry the runtime integration? Yes — and it already has PRs

Correction to the 2026-09-12 record, which recommended opening PRs: **both already exist.**

```
$ gh pr list --state all --limit 15
  229 OPEN codex/market-hours-monitoring   fix(ops): observe market open after overnight daemon starts
  230 OPEN codex/equity-reconciliation     fix(reconciliation): accept price-explained equity changes ...
```

`claude/trial-runtime-integration` duplicates `5025b21` as a merge and remains verification evidence
only. Do not merge it.

## 5. What `claude/status-file-deadlock` fixes, stated precisely

The fix is right; one sentence of its reasoning is not. The commit says a bare relative path resolves
"against the process working directory, which for a deployed service is the repository". The service
wrapper `chdir`s to the **state directory** before running a component, which is why
`D:\Elrond\state\PROJECT_STATUS.md` exists and reads `Generated at: 2026-09-11T20:05:03Z` — the
daemon's Friday cycle. The checkout's copy reads `2026-09-12T15:15:36Z` (mtime 11:15:36.44 EDT), so it
was written by some process whose working directory *was* the checkout, 0.4 s before ingestion's first
refusal. Only `doctor` and `run-once` call `write_project_status`; which process ran it is **UNKNOWN**
from the evidence available (no log names it). The fix writes to `runtime.paths.reports` regardless of
working directory, so it closes the deadlock for any caller, including that one.

The deployed checkout still shows ` M PROJECT_STATUS.md` at `2026-09-13T02:58Z`, and
`Elrond Ingestion`, `Research` and `Report` are still refused with `CHECKOUT_NOT_CLEAN` every run.

## 6. Verified suites

Every suite: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider`, full, in a worktree whose venv
is built on `D:\Elrond\tools\python\cpython-3.11.16-windows-x86_64-none` (see the 2026-09-12 note on
`%APPDATA%` interpreters). The exit code is pytest's own, captured with no pipeline.

| branch | SHA | UTC start | UTC end | s | passed | skipped | failed | exit |
|---|---|---|---|---|---|---|---|---|
| `codex/market-hours-monitoring` (PR #229) | `c45be7e` | 00:06:35 | 00:15:47 | 552 | 2822 | 45 | 0 | 0 |
| `codex/equity-reconciliation` (PR #230) | `9b44885` | 00:16:15 | 00:25:27 | 552 | 2846 | 45 | 0 | 0 |
| `claude/status-file-deadlock` | `4f4c28b` | 00:25:58 | 00:34:40 | 522 | 2818 | 45 | 0 | 0 |
| `claude/windows-sandbox-backend` | `88d2f6a` | 02:35:17 | 02:44:38 | 561 | 2851 | 46 | 0 | 0 |
| `claude/research-proposer` | `ac546f3` | 02:59:16 | 03:08:47 | 571 | 2916 | 46 | 0 | 0 |
| `claude/integration-verify-2026-09-13` (all of the above merged) | `cd57f7b` | 03:09:31 | 03:19:24 | 593 | 2950 | 46 | 0 | 0 |

All times 2026-09-13. PR #230's 2846/45 matches the deployed runtime's reported figure. The one
extra skip on the sandbox-bearing heads, found by diffing the skip lists, is
`tests/unit/sandbox/test_backend_selection.py:118` — *the refusal under test is the non-Windows one*.

A skip worth knowing about, present on every head tested:
`tests/integration/test_generated_experiment.py` (2 tests) is pinned to `WslSandbox`, so the
model-generated-code path (#8) is never exercised under the Windows backend on this host even though
`codegen.py` accepts any OS-enforced backend. The autonomous proposer does not use that path; it is
a coverage gap, not a defect shown to exist.

`claude/integration-verify-2026-09-13` @ `cd57f7b` is `origin/main` (`7dc26dd`) with `--no-ff`
merges of `codex/equity-reconciliation` (`9b44885`), `claude/status-file-deadlock` (`4f4c28b`) and
`claude/research-proposer` (`163409a`), in that order, with no conflicts. It is pushed as verification
evidence only — integrate through the individual PRs. `163409a` differs from the suite-tested
`ac546f3` only under `docs/` and `README.md` (`git diff --stat ac546f3 163409a -- src tests` is empty),
and this table was filled in by a later docs-only commit.

## 7. Merge order

| # | Branch | PR | Why its own step |
|---|---|---|---|
| 1 | `codex/market-hours-monitoring` | #229 (open) | Closed-market quiet stops reading as a fault |
| 2 | `codex/equity-reconciliation` | #230 (open) | The deployed reconciliation fix. Stacked on 1: after 1 merges, #230 shows its own 2 commits |
| 3 | `claude/status-file-deadlock` | to open | Ends the `CHECKOUT_NOT_CLEAN` self-lock. Independent of 1–2 |
| 4 | `claude/windows-sandbox-backend` | to open | OS-enforced isolation. Independent |
| 5 | `claude/research-proposer` | to open | The autonomous producer. **Contains 4**; open it after 4 merges so its diff is its own 13 commits |

**3 before any unattended deployment.** **5 carries migration 0028**: after deploying it, run
`quantbot migrate-database --confirm` against the live store before restarting services, or every
component refuses a store behind head (by design, #66). Take a `backup-database` first.

Immediate operational note, not done here because it changes the deployed host: until 3 is deployed,
the dirty `PROJECT_STATUS.md` in `C:\Users\leepa\elrond-omen-runtime` keeps refusing ingestion,
research and report on every run, and would refuse the daemon, protection and console after a reboot.
Restoring that one generated file (`git -C C:\Users\leepa\elrond-omen-runtime restore PROJECT_STATUS.md`,
after copying it aside) clears the refusal until something writes it again.
