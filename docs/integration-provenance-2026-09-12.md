# Branch provenance and the integration order — 2026-09-12

Written because a previous report recommended opening a pull request from a branch that does not
exist on the remote. It does exist, but only as a local branch in a second clone, and the
difference matters: you cannot open a PR from it.

All commands below were run at `2026-09-12T21:51Z`–`21:53Z` from `D:\GitHub\project_elrond`.

## The three questions, answered

### 1. Why is local `main` at `c817094` while `origin/main` is at `7dc26dd`?

```
$ git rev-parse main origin/main
c817094a4bfd8aa68d1e41dd0cc0d8d8fe9eafc3
7dc26dd99b1d9b48ced879f5699afa696f9cef2c

$ git merge-base --is-ancestor c817094 origin/main && echo ancestor
ancestor

$ git rev-list --count c817094..origin/main   # behind
59
$ git rev-list --count origin/main..c817094   # ahead
0
```

Local `main` is **stale, not divergent**: 59 behind, 0 ahead, and a strict ancestor. It has simply
never been fast-forwarded. Nothing is lost and nothing conflicts. It was left untouched.

### 2. Where is `codex/omen-paper-runtime`?

Not on the remote:

```
$ git ls-remote --heads origin | grep omen-paper-runtime
(no output)
```

It is a **local branch in a different clone**. The deployed runtime is not a clone of its own — it
is a worktree belonging to a second checkout under OneDrive:

```
$ git -C C:/Users/leepa/elrond-omen-runtime rev-parse --git-dir
C:/Users/leepa/OneDrive/Documents/GitHub/project_elrond/.git/worktrees/elrond-omen-runtime

$ git -C C:/Users/leepa/OneDrive/Documents/GitHub/project_elrond worktree list
C:/Users/leepa/OneDrive/Documents/GitHub/project_elrond  adc6139 [main]
C:/Users/leepa/elrond-local-account-evidence             46e3692 [codex/continuous-research]
C:/Users/leepa/elrond-omen-runtime                       5025b21 [codex/omen-paper-runtime]
C:/Users/leepa/elrond-runtime-recovery-190               4408b96 [astra/runtime-recovery-190]
```

So there are two clones of this repository on the Omen, and the deployed commit sits on an
unpushed local branch of the one this session was not working in. **The earlier recommendation to
open a PR from `codex/omen-paper-runtime` was wrong** and is withdrawn.

That clone also holds `codex/continuous-research` (`46e3692`), an unpushed docs-only commit
carrying a plan and design spec for continuous PAPER research. It contains no implementation. The
proposer built here follows its constraints deliberately — a finite versioned catalogue, a model
that selects rather than invents, provenance and idempotency, exploratory and confirmatory data
kept apart — while being much smaller than its three-task scope.

### 3. Does the deployed work already exist on the remote?

Yes, under different names. The four commits past the already-merged base are a linear stack, and
two remote branches carry it:

```
302bf8e  (already on main, via the #228 merge at 7dc26dd)
 └─ f125bd5  fix(ops): monitor market open after overnight daemon starts      [correctness]
    └─ 477f885  docs(ops): reboot recovery and market-hours deployment        [docs]
       └─ c45be7e  docs(ops): opening fills and protection recovery           [docs]
          │                                    ← origin/codex/market-hours-monitoring
          └─ 5025b21  fix(reconciliation): explained equity marks vs drift    [correctness]
             │                                 ← DEPLOYED HEAD
             └─ 9b44885  docs(ops): verified equity fix and measured clearance [docs]
                │                              ← origin/codex/equity-reconciliation
                └─ aa4aafb  Show verified market status …                     [console]
                   └─ d191c01  Cover clock skew …                            [console]
                                                ← origin/codex/console-market-status
```

`claude/trial-runtime-integration` duplicates this content as a merge of the deployed tip into
main. It was verification evidence and is now **redundant — do not merge it**; the native branches
carry the same commits with their original authorship.

`origin/codex/console-market-status` adds two console commits beyond the deployed state. Out of
scope here, and not part of the recommended order.

## Ancestry of the branches built in this work

```
$ git merge-base origin/main origin/claude/windows-sandbox-backend   -> 7dc26dd  (based on main)
$ git merge-base origin/main origin/claude/status-file-deadlock      -> 7dc26dd  (based on main)
```

`claude/research-proposer` is based on `claude/windows-sandbox-backend`, not on `main`, because
its measurement stage calls `select_isolated_backend()`. That is a real dependency, not an
accident of branching.

## Recommended integration order

Verified by merging each in sequence into a throwaway worktree off `origin/main` with
`--no-ff`: **no conflicts at any step**.

| # | Branch | Adds | Why it is its own PR |
|---|---|---|---|
| 1 | `codex/market-hours-monitoring` | `f125bd5`, `477f885`, `c45be7e` | Market-hours correctness; stops closed-market quiet being read as a fault |
| 2 | `codex/equity-reconciliation` | `5025b21`, `9b44885` | Reconciliation correctness; the deployed fix. Builds on 1 |
| 3 | `claude/status-file-deadlock` | 4 commits | Stops the generated status file refusing every service. Independent |
| 4 | `claude/windows-sandbox-backend` | 5 commits | OS-enforced isolation on this host. Independent |
| 5 | `claude/research-proposer` | 7 commits | The autonomous producer. **Depends on 4** |

1 and 2 are a stack: merging 1 first leaves 2 with two commits. 3 and 4 are independent of
everything and of each other. 5 must follow 4.

## What was verified before recommending any of it

| Head | Suite | Result |
|---|---|---|
| `claude/windows-sandbox-backend` @ `17ac5c4` | full | 2851 passed, 46 skipped, 0 failed, exit 0 |
| `claude/status-file-deadlock` @ `4f4c28b` | full | 2818 passed, 45 skipped, 0 failed, exit 0 |
| `claude/trial-runtime-integration` @ `adccca0` | full | 2846 passed, 45 skipped, 0 failed, exit 0 |
| 1+2+3+4 merged @ `9a721be` | full | 2885 passed, 46 skipped, 0 failed, exit 0 |

The trial merge's 2846/45 matches the deployed runtime's own reported figure exactly, which is
the cross-check that the four commits reproduce what is running on the Omen.

## A note on running the suite here

`deploy/omen/setup-windows.ps1` refuses with `VENV_BASE_NOT_HOST_VISIBLE` when a virtualenv's
*base* interpreter lives under `%APPDATA%`, which is where `uv` puts its managed Python by
default. The deployed runtime's venv is built on `D:\Elrond\tools\python\cpython-3.11.16-…`
instead, deliberately outside the profile.

A worktree venv built the default way therefore fails three tests that have nothing to do with the
change under test. Build it against the same base interpreter and they pass:

```
uv venv --python D:\Elrond\tools\python\cpython-3.11.16-windows-x86_64-none\python.exe
uv sync --frozen
```

## Nothing was rewritten

No force-push, no history rewrite, no branch deleted, no reset of any existing branch, and `main`
was not changed — locally or remotely. `claude/research-proposer` was re-based onto the sandbox
branch before it had any commits, by stashing uncommitted work and resetting a branch that pointed
at nothing of its own.
