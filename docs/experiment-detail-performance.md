# Experiment detail journal reuse

The experiment detail panel used the ordinary read-only store independently in
its evaluation, each exploration scorecard, lot resolver and feedback provenance
reader. Those nested readers repeatedly queried and hash-checked the same complete
as-of journal. A bounded isolated profile on existing read-only input reproduced
29.362 seconds total, including 40 full journal loads taking 24.403 seconds.

The console now creates a request-local read-only store that memoizes complete
hash-checked event tuples by experiment identity and exact as-of datetime. The
first load still uses `ExperimentStore.read_as_of`; no event is dropped, and all
existing decoding, economic scoring, ownership, calendar and provenance checks
remain in their ordinary readers. Cross-experiment lot discovery remains intact.
Each new panel request creates a fresh cache, so later appended or corrupted
receipts are re-read. State and outbox reads retain their existing behavior;
this is not a global transaction snapshot of those separate tables.

The query-bound regression originally failed with 20 complete journal queries
for four registered policies; the repaired reader uses one. Regressions compare
ordinary and cached evaluation, scorecards and readiness, check earlier cutoffs
and new receipts, reject corrupted evidence, and preserve conflicts when a lot
identity appears in another experiment. Existing HTML/JSON and scorecard tests
are included in focused validation.

A second bounded read-only isolated profile measured 4.866 seconds, with one full
journal load taking 0.587 seconds. The journal continued to accumulate a small
number of calendar/recovery receipts between profiles, so these timings are
observations on comparable evolving input rather than a controlled benchmark.
No live HTTP request, provider call or operational write was made for profiling.

Runtime acceptance remains necessary after an authorized rollout: verify both
experiment detail HTML and JSON within the acceptance timeout, including verified
scorecards, unknown evidence and unchanged authority boundaries. Source tests and
isolated profiling do not establish deployed acceptance.
