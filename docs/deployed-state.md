# Deployed state: what is actually running

> **Current PAPER runtime, October 15:** The approved affordable-stock/whole-share entry policy is active on the Omen. Fractions remain preferred; an exact one-share fallback keeps the existing loss budget. The first AAPL round trip is verified; fees/net remain unknown and no learner fit is claimed. See the [current handover](evidence/paper-core/whole-share-2026-10-15/README.md).

## Current deployment boundary — October 15, 2026

The core is commissioned; guardian, outcomes and trader are active after fresh independent progress and protection checks. The [readiness repair](evidence/paper-core/readiness-2026-10-15/README.md) records exact pins and checks.

## Earlier staging snapshot — October 14, 2026

The accepted [PAPER core](paper-core.md) baseline is prepared, not functionally deployed. Read the
[current Omen staging record](evidence/paper-core/omen-deployment/README.md) and
[handover](evidence/paper-core/handover.md) for exact release/configuration hashes and evidence.
The prepared release uses schema 36 and Mako 1.4.3. Only restored operational and learner
copies migrated from schema 29; their original values and identities were preserved.
No operating migration or runtime commission has occurred.

Trader, guardian and outcomes tasks are registered disabled, with submission/entry clamps
and no triggers. The verifier passed under the actual `leepa / Interactive / Limited`
identity with Windows PowerShell `CurrentUser RemoteSigned`; no Bypass was introduced.
Existing protection/deadline services are unchanged. Nine inherited positions retain
`adaptive-momentum-v1-309894d8d8a5296e` ownership. The October 15, 01:44 UTC read found an
accepted unfilled IWM sell of 0.169669 shares during the closed market, preventing the
required no-open-order commissioning. Reacquire evidence before any later action.

A checkout, successful verifier, registered task or live process does not establish a
commissioned model, attributable fill, reconciled outcome or profitability. Core identity
separates release, economic policy, immutable model, outcome revision and commissioned
deployment. Compatible executable maintenance preserves economic identity only through an
audited binding. Routine scoped waits do not clear manual stops or integrity quarantine.

The [approved daily successor](superpowers/specs/2026-10-14-daily-paper-exploration-design.md)
is distinct from that staged XLE package. It adds hybrid stock selection with six durable
daily reservations, V3 executed-gross learning and `BROKER_PAPER_BALANCE_V1` current-funds
authority. It merged in PR #290 and is commissioned locally with the submission readiness
repair. Guardian/outcomes readiness and inherited protection pass, and entries are resumed.
Fresh IEX bars remain unavailable, so new submissions/fills and daily fits are unverified.
The combined trading-to-learning witness is offline evidence. Use the
[current submission package](evidence/paper-core/submission-2026-10-15/README.md) for native pins.

This economic change requires a successor manifest/configuration and target/funds-specific
initial commission from COLLECT; a code-only compatible binding cannot grant it. Keep V2 net
and V3 gross models/receipts distinct. A current-funds receipt authorizes bounded sizing only,
leaving historical cash incomplete and fees/net unknown when evidence is missing. Report
successor verification and runtime status separately from every baseline fact above.

## Legacy deployment inventory mechanism

The remaining sections describe the retained inventory API and historical observations.
Their schema/commit/status values are not current rollout pins. Legacy executables expect
schema 29 at startup; do not restart them against schema 36 without a compatible protection
and exit transition. The core's own status/export and role heartbeats provide separate
release, authority, progress and first-blocker evidence after commissioning.

**Issue:** #190 · **Historical schema at introduction:** 27 · **Command:** `quantbot deployment-status`

Historical Omen observations are in [the September 10 runtime record](omen-runtime-2026-09-10.md).
Local-account application startup tasks run from commit `5025b21586223d05dfeb4c37c2a514338a25a711` against
`D:\Elrond\state\quantbot-paper-fresh-20260908.db`. PAPER reconciliation and readiness pass, and
the durable kill switch is clear after the tested equity-valuation update and measured clearance.
This document describes the evidence mechanism; its historical inventory below
must not be read as the current host's state.

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
trading_mode  store_identity  schema_version
strategy_id  strategy_version  configuration_hash
process_started_at  observed_at
```

Recorded **before** anything can halt. A coordinator that stops at reconciliation has still run,
and "what is running" is not "what succeeded" — an observation written only on a clean cycle
would go missing exactly when an operator most needs to know which checkout was up.

Append-only, not mutable current state, for one reason: **a coordinator that died cannot update
its row to say so.** Absence of a recent observation is the signal, and a crashed process cannot
suppress it. Overwriting would also erase the sequence needed to see a known commit reappear
after a different known commit — the A → B → A checkout-reversion signal worth keeping.

**Nothing recorded is secret.** `store_identity` is a random UUID persisted in the singleton
`schema_version` row. It is a durable store-lineage identity: a byte-for-byte backup or moved copy
preserves it, but it does not prove that two files are the same physical file. It contains no path,
hostname, credential, account identifier, or other deployment secret.

Migration 0027 intentionally cannot downgrade to 0026. The upgrade replaces historical path
hashes with the lineage UUID, so the old hashes cannot be reconstructed truthfully. Relabeling a
UUID as `database_fingerprint` would make older code read it as a SHA-256 path hash. The downgrade
therefore fails before changing the schema and directs the operator to a pre-0027 backup.

## The warnings

| Warning | Means |
|---|---|
| `NO_RUNTIME_RECORDED` | nothing has ever said what it is running — the state #190 found |
| `NO_ACTIVE_COORDINATOR` | nothing observed within 100 hours; the last one seen is still readable |
| `MULTIPLE_ACTIVE_COORDINATORS` | two recent same-host recorded intervals overlap, or cross-host wall-clock intervals are consistent with overlap |
| `STALE_CHECKOUT` | the current known commit reappeared after a different known commit (A → B → A) |
| `UNIDENTIFIED_CHECKOUT` | the coordinator did not record a commit, so staleness cannot be judged |
| `SCHEMA_VERSION_MISMATCH` | an active runtime recorded a schema this build does not have |
| `LIVE_TRADING_RECORDED` | a runtime reported `LIVE` mode |

**Historical reversion detection needs no network.** Only reappearance establishes a reversal
from observations alone, without pretending commit names encode Git ancestry.
A → B is a normal change and A → B → C is another forward history; neither is stale.
A → B → A is stale because A appeared, a different known commit ran, and A then reappeared. That
is precisely the hazard the `E:\...\quantbot-watchdog.cmd` fix closed at the source, and this makes
a recurrence detectable rather than only prevented.

`UNIDENTIFIED_CHECKOUT` is deliberately separate from `STALE_CHECKOUT`. A runtime that will not
name its commit is *unknown*, not *proven stale*, and collapsing the two would either invent a
problem or hide one. Runtime construction now captures the installed source checkout's full
HEAD once, independent of cwd and later environment changes. Installed wheels can supply a full
`QUANTBOT_GIT_COMMIT` pin; missing or invalid pins remain UNKNOWN. The strategy deployment's
older commit is not reused as the running code's identity.

## Recovery inventory and intended deployment

`quantbot deployment-status` now runs before runtime construction, including when the database
is missing or older than this build. `--inventory` adds bounded host/startup checks, `--candidate`
and `--scan-root` discover comparison stores, and `--intended-commit` overrides the local
`origin/main` reference. A full report and the exact recovery procedure are in
[`runtime-recovery.md`](runtime-recovery.md).

The existing status reader additionally compares recorded SHA to deployment intent. An ancestor
is `BEHIND`/`STALE_CHECKOUT`; other differences are `DEPLOYMENT_INTENT_MISMATCH` with their
relationship stated. Unknown intent is `UNKNOWN_DEPLOYMENT_INTENT`. History alone cannot tell
whether a new main commit was never deployed, which is why both checks are necessary.

The CLI envelope is a recovery assessment: `ok=false`/exit 1 while global authority remains
unverified. A nested historical reader's `ok` describes its recorded warnings only. It grants
no startup authority. Legacy `active` fields mean recently observed; explicit liveness and
coordinator/store authority fields remain UNKNOWN without independent measurement.

The 100-hour activity bound is `scripts/supervisor.py::STALE_RUN_HOURS` — an existing deployed
value, CLAUDE.md's first tiebreak for an unspecified threshold — chosen there because consecutive
cycles are 96h apart across a Monday holiday. The second-writer warning does not treat that
freshness alone as liveness: it requires observations from both processes whose recorded intervals
strictly cross each other's `process_started_at`. Intervals that only touch are a handoff boundary,
not established concurrency, so a recent observation at or before a replacement starts is a normal
restart.

For two processes on the same host, the warning reports that their intervals overlap on the
host's recorded clock. For processes on different hosts, separately corrected wall clocks cannot
literally prove concurrency. The warning therefore says only that the cross-host timestamps are
*consistent* with overlap and explicitly names clock skew/NTP uncertainty. Non-overlapping
cross-host timestamps do not produce the warning, but they also do not prove the hosts were never
concurrent. No OS process inspection, network call, or deployment authority is implied.

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
