# PAPER relative-clock contract review

Status: source implementation approved and in validation as of October 13, 2026.
Hutch approved the bounded PAPER-only two-second relative-clock replacement and
verified deployment through the end of October 13 America/New_York. This document
records the earlier rationale and the current source contract; it does not claim
deployment, stop clearance, or actual broker/Windows health. OS settings and LIVE
behavior remain unchanged. STRICT_V0 remains the default; RELATIVE_V1 must be
explicit in both the execution policy and independent deadline launcher.

The execution policy is part of model configuration binding. The standalone
deadline launcher's selected clock contract is a separate operational profile,
audited in each heartbeat/run. RELATIVE_V1 entry refuses a heartbeat using another contract;
the launcher argument does not itself confer predictive-model approval.

Synchronization evidence must be fresh at final assessment and every reuse, under
the lesser of 2048 seconds and two configured poll periods. A synchronization
timestamp future-dated when queried remains invalid after collection. Source
tests preserve that chronology refusal, wall/monotonic continuity, expiry, raw
advancing samples and unchanged future-feature gates.

## Verified source contract

`learner/paper_deadlines.py:331` requires a broker clock timestamp whose age,
measured against local wall time after the GET response, lies in `[0,120]` seconds.
`learner/paper_runner.py:366` applies the same zero-future condition before entry,
using the frozen reference-age maximum. Repairing only the deadline worker would
therefore not establish entry readiness.

The zero-future rule existed at `6f09249`; `2e35e8f` moved the sample to after the
response and added rejection of a reversed local clock. The latter change fixed
sampling chronology while retaining the bound. The pilot close contract explicitly
preserves clock/freshness limits and requires repairing unsynchronized hosts rather
than widening those limits. Existing deadline tests reject a future timestamp.
The isolated clock-semantics tests additionally reject one microsecond of provider
lead and demonstrate correct post-response sampling without any real network I/O.

The source safety rationale is conservative chronology: a time sample that appears
to come from the future cannot establish current deadline/entry readiness. Entry
references, session eligibility, fill chronology, account ownership and protection
must remain independently verified. I found no documented engineering guarantee
that two independently synchronized clocks have zero relative error or that the
local clock always runs later than the provider.

## Existing approved designs do not solve this generally

- The console's two-second allowance is explicitly informational and grants no
  execution authority. It cannot be borrowed as an approved trading tolerance.
- Account-snapshot capture uses local receipt timestamps and bounded acquisition.
  This avoids one cross-clock age calculation but does not establish broker clock
  correctness, session bounds or independent deadline readiness.
- The owned-fill clock-domain correction at `4b546b7` uses the broker submission
  timestamp as the chronology floor for owned risk-reducing exits. It retains the
  separate clock gate and does not create an alternative entry clock source.
- The deployed-state timing comparison reports overlap/clock uncertainty; it is
  informational and does not grant permission or certify UTC accuracy.

Let local and provider clock errors be `e_local` and `e_provider`, and actual
response delay be `d`. A fresh response has observed age
`d + e_local - e_provider`. A small legitimate provider lead greater than the
response delay makes that age negative. Synchronization can reduce clock errors
without guaranteeing their ordering. Repeated resync, an artificial delay,
clamping, offsetting local time, or substituting the broker timestamp for local
time would not prove that the existing zero-future contract had been satisfied.

Microsoft documents residual errors from network asymmetry and limits of remote
time references. NTP itself explicitly models clock offset, delay and uncertainty.
These support the general limitation, not a diagnosis of the Omen or the broker:

- https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/accurate-time
- https://www.rfc-editor.org/rfc/rfc5905.html

## Proposed reviewed PAPER-only replacement

This contract change received specific approval. Independent review and exact-head
validation remain required before deployment; it was not inferred from permission
to retain earlier tolerances.

1. Introduce one versioned clock-evidence contract used by both the entry runner
   and deadline worker. Retain raw provider/local timestamps; record monotonic
   request/response duration, pinned provider/account identity and evidence version.
   Never modify an OS clock or manufacture a timestamp.
2. Evaluate relative-clock consistency separately from response freshness. Candidate
   maximum absolute relative skew: **two seconds**, proposed for explicit review
   because that magnitude already exists in informational diagnostics. It is a new
   execution policy, not inherited authority or a value fitted to one successful
   sample. Keep the existing **120-second maximum**; reject transport over the
   existing **five-second bounded acquisition** budget and unavailable measurements.
3. Require two advancing, mutually consistent broker-clock samples within that
   acquisition budget, with fresh independently available Windows synchronization
   health. If the production account cannot read that health without new access,
   report a separate setup dependency; never elevate the whole trading service or
   accept an old user-provided status as continuing attestation. Define and review
   the sync-evidence freshness requirement before implementation.
4. Reject backward local time, replayed/constant samples, large skew, expired
   evidence and calendar disagreement. When timing uncertainty overlaps market
   open/close, reference expiry or a holding deadline, refuse new entries; use
   conservative timing for risk-reducing protection rather than extending a hold.
5. Keep future price bars, features, predictions and research evidence rejected
   under their current contracts. Broker-clock metadata must not grant permission
   to consume future market data, alter holdouts or borrow stale intent authority.
6. Retain all per-order risk, ownership, protection, reconciliation, manual pause
   and atomic claim checks. LIVE behavior remains unchanged. Preserve raw failure
   history and classify clock inconsistency separately from stale provider data.

Risks: relative agreement is not proof of true UTC; two samples from one provider
can share an error. Provider clocks and calendars may have correlated failures.
Windows sync health alone does not bound broker-relative error. Multiple probes
add latency, and any timing ambiguity must remain a refusal. The proposal does not
resolve accounting differences or the newer reconciliation stop.

## Required tests and acceptance

- Boundary tests for the explicitly approved skew/age/transport bounds in both
  directions, stale and repeated samples, unavailable sync evidence, local reversal,
  wall/monotonic discontinuity, restart and expired evidence.
- Actual entry and deadline paths sharing the same evidence contract; no authority
  from console status. Refuse ambiguous market open/close and reference/horizon edges.
- Future market-data/feature/holdout refusal remains unchanged. Proven owned-fill
  chronology and provider-domain validation must still refuse mismatched/early/
  future fills; do not widen fill acceptance implicitly with the metadata change.
- Manual pause before atomic submission blocks it. Unknown submissions remain
  read-only recovery and cannot cause stale-intent replay or duplicate POSTs.
- Independent source review, required frozen dependencies, exact-head aggregate
  and asynchronous integration checks, followed by a separately approved bounded
  rollout with real post-deployment receipts. Current restricted test-harness and
  host-read limitations remain blockers to those claims.

## Policy-respecting native test harness

Synthetic launcher tests no longer override execution policy. File-based fixtures
use the first existing supported test engine only after querying its actual policy;
an engine that refuses unsigned local fixtures fails the prerequisite rather than
trying another engine after refusal. Policies and security settings are unchanged.

The native service-host implementation still starts Windows PowerShell. Its GUI,
logging, native-argument transport and process-tree tests use ordinary explicit
inline fixture commands, without loading or evaluating a denied script file.
A separate real `-File` fixture checks the host honors its actual script policy:
Restricted/AllSigned refuses the unsigned fixture and leaves its marker absent.
Permitted policies run the local fixture normally; the policy must remain unchanged.
This does not claim production script-file launch works under Restricted.

Scheduler tests retain fake task lifecycle functions and disabled module autoload;
they record synthetic changes rather than registering, stopping or altering deployed
tasks. Launcher modules and stores are fake files below the test temporary root;
model processes are fixture commands only. Host cleanup targets only fixture-owned
process IDs. No broker adapter, deployed store or model promotion is involved.
Native stderr, exit codes, ownership refusal, scheduler limits, argument quoting,
no-console behavior and child cleanup assertions remain in place. Log decoding
accepts the selected shell's UTF-16 BOM or native UTF-8 bytes without changing
asserted content or secret-removal expectations.

Real rollout still needs current launcher, clock, deadline, reconciliation,
protection, ownership and exact binding verification. A passing synthetic suite
does not provide that operational evidence.

No source proposal or passing synthetic test clears the existing stop or proves
that a natural eligible PAPER cycle has executed.
