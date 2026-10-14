# Independent M0 review

Reviewer: fresh-context `/root/m0_review`, not the implementer. Scope: new code,
tests, plan/spec, selected saved integration evidence. No production or broker
queries by the reviewer. Parent is the integration owner.

Initial verdict: **REWORK**, no Critical findings, two Important and one Minor:

1. Standalone bundle omitted account identity, and accountless LotEvents inherited
   the configured account. Fixed by requiring and matching observed LotEvent hashes,
   retaining them in events and bundle, and distinguishing configured account from
   journal evidence. Missing and mismatched account tests cover the boundary.
2. First-blocker view discarded recorded reasons. Fixed with an explicit optional
   local receipt path, allowlisted status/reason, timestamp/digest/release/age, and
   preserved historical cycle reasons. Recorded stop takes precedence; broker
   freshness remains NOT_PROBED. No stop is cleared or reclassified.
3. Malformed host collections aborted export. Fixed by reporting collection gaps
   while retaining store evidence; null, string and null-element cases tested.

Owner verified each finding against source and reproduced failures before fixes.
A patch-placement mistake during the fix was caught by fixture tests/type checks
and corrected before re-review.

Final technical verdict: **ACCEPT**, no remaining Critical or Important findings.
Reviewer independently ran all 15 focused tests (5.07 s) and checked refreshed
evidence at cutoff `2026-10-14T10:38:28.907218+00:00`. Recorded entry receipt is
`MARKET_CLOSED / NORMAL_WAIT` at `2026-10-14T10:36:40.439666Z`; historical halted
cycle remains separately timestamped. Costs and learning gaps remain explicit.

Out of M0 scope: fresh broker ownership/readiness, fee verification, protection
certification/repair, numerical learning/activation, exact historical producing
release certification, atomic cross-store reconstruction. WAL read proves committed
WAL visibility and unchanged fixture main file; no byte-for-byte immutability of
SQLite shared-memory lock sidecars is claimed.

Integration-owner full-suite gate is recorded separately in README.md. Technical
review acceptance is not deployment approval or completion of M1–M6.
