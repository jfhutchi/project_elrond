# Deployed state: what is actually running

**Issue:** #190 · **Schema:** 26 · **Command:** `quantbot deployment-status`

---

## Why this exists

The #190 inventory of the HP Omen answered four questions by archaeology:

| Question | How it was answered on 2026-09-02 |
|---|---|
| Which commit is checked out? | `git reflog`, which happened to still hold the clone and every checkout since |
| Is anything running? | `Win32_Process`, scheduled tasks, registry `Run` keys, Startup folders, services |
| Which database is authoritative? | a filesystem-wide search for `*.db` that found none |
| Could a second host wake up as a writer? | reading a checked-in `.cmd` file and noticing it named a drive that does not exist |

Every one of those answers was unambiguous **only because nothing was running**. With a live
process on an older checkout, the process table says a python is up but not which tree it came
from, the reflog says what the tree is now rather than what the process loaded, and two databases
on two hosts are indistinguishable from one.

So the runtime states its own identity, and a reader turns the observations into the warnings
that matter.

## What is recorded

One append-only observation per cycle, written while the coordinator holds the writer lock, in
the same transaction as the run row:

```
host_id  instance_id  role  git_commit  package_version  python_version  platform
trading_mode  database_fingerprint  schema_version
strategy_id  strategy_version  configuration_hash
process_started_at  observed_at
```

Recorded **before** anything can halt. A coordinator that stops at reconciliation has still run,
and "what is running" is not "what succeeded" — an observation written only on a clean cycle
would go missing exactly when an operator most needs to know which checkout was up.

Append-only, not mutable current state, for one reason: **a coordinator that died cannot update
its row to say so.** Absence of a recent observation is the signal, and a crashed process cannot
suppress it. Overwriting would also erase the evidence that an older checkout ran at all, which
is the stale-host tell worth keeping.

**Nothing recorded is secret.** The database is identified by a SHA-256 of its resolved path
rather than by the path: that answers "is this the same store?" across hosts without publishing a
filesystem layout. No credential, key or account identifier is read.

## The warnings

| Warning | Means |
|---|---|
| `NO_RUNTIME_RECORDED` | nothing has ever said what it is running — the state #190 found |
| `NO_ACTIVE_COORDINATOR` | nothing observed within 100 hours; the last one seen is still readable |
| `MULTIPLE_ACTIVE_COORDINATORS` | two processes are active; the second-writer condition, visible |
| `STALE_CHECKOUT` | a commit that ran here before is not the one running now |
| `UNIDENTIFIED_CHECKOUT` | the coordinator did not record a commit, so staleness cannot be judged |
| `SCHEMA_VERSION_MISMATCH` | an active runtime recorded a schema this build does not have |
| `LIVE_TRADING_RECORDED` | a runtime reported `LIVE` mode |

**Stale-checkout detection needs no network and no deploy manifest.** The history *is* the
manifest: a commit observed here earlier, which is not the one observed now, means something went
backwards. That is precisely the hazard the `E:\...\quantbot-watchdog.cmd` fix closed at the
source, and this makes a recurrence detectable rather than only prevented.

`UNIDENTIFIED_CHECKOUT` is deliberately separate from `STALE_CHECKOUT`. A runtime that will not
name its commit is *unknown*, not *proven stale*, and collapsing the two would either invent a
problem or hide one. Set `QUANTBOT_GIT_COMMIT` to make the distinction go away.

The 100-hour activity bound is `scripts/supervisor.py::STALE_RUN_HOURS` — an existing deployed
value, CLAUDE.md's first tiebreak for an unspecified threshold — chosen there because consecutive
cycles are 96h apart across a Monday holiday.

## Nothing here has authority

It reports; it does not gate. A stale checkout is a fact an operator needs, not a reason for
software to refuse, and a deployed-state check that could halt trading would be a new way for the
system to stop itself over a bookkeeping disagreement.

`LIVE_TRADING_RECORDED` is the sharpest case. It is a warning and only a warning: live trading
already has its own gates that no agent may pass, and a second, weaker mechanism next to them
would be a place for the real one to be worked around.

## Worker availability is not coordinator authority

`role` separates `COORDINATOR`, `WORKER`, `CONSOLE` and `TOOL`, and `deployment-status` reports
active workers in their own list. A Kronos worker being up says nothing about whether anything is
trading, and reading one as the other is how "the system is running" becomes true and useless at
the same time.

## Last successful cycle

Read from the `runs` ledger rather than stored again. A second copy of a timestamp is a second
thing that can be wrong, and the run ledger is the authority on whether a cycle finished. A
halted cycle after a successful one does not become the last success.

## A clock that steps backwards

`process_started_at` is real wall clock; `observed_at` comes from the caller's clock. An NTP
correction or a VM resume between the two can invert them, and this is built while the
coordinator holds the writer lock at the top of a cycle.

So it clamps rather than raises: aborting a trading cycle over a bookkeeping timestamp is worse
than recording "up since no later than now". The clamp is visible rather than silent — the two
timestamps come out exactly equal, which they otherwise never would.
