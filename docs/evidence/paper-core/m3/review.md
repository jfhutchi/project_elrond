# M3 independent review

Reviewer: `/root/m3_review`, independent Astra. Reviewed base `e7b1eae28c0e5e6e49173facda55ad9345e3a86a` through implementation `6d796a62925145c35909225f34d9574264ce64ec`.

## Initial verdict: REWORK

Spec compliance has three issues; code quality needs fixes. The reviewer accepted the retained transactional final-send/state transition, append-only recovery with newer cash proof, joined five-POST/two-costed-lot evidence and populated-v31 identity-preserving migration checks, subject to these findings.

1. **P1: healthy inventory acquisition latency blocks entry.** `src/quantbot/paper_core/trader.py:73` evaluates inventory age at the earlier time acquisition anchor, while `guardian.py:60` timestamps the inventory proof after awaited synchronization. Just 1 ms on each account fetch produces decision time 14:00:00.020 and proof time 14:00:00.022, causing `INVENTORY_STALE` and zero POSTs. Use a current validated projection after synchronization, preserve immutable acquisition evidence and reproducible claim validation, and test positive latency plus stale/delayed refusal. [Expected-red reproduction](review-inventory-latency-probe.txt).
2. **P1: near-close monotonic fallback mixes time domains.** `src/quantbot/paper_core/time_evidence.py:202` subtracts local-wall `recorded_at_utc` from a provider-domain deadline. At +1.9 s skew with the close buffer limiting the hold, a clock outage leaves the guardian waiting after the deadline is already due. The probe has provider time 120 ms past deadline after 8.2 monotonic seconds, with no exit. Persist a conservative monotonic expiry/duration derived in matching domains, preserving old payload IDs, and exercise both skew directions during timing outage. [Expected-red reproduction](review-close-deadline-probe.txt).
3. **P2: ordinary waits precede genuine integrity incidents.** `src/quantbot/paper_core/health.py:103` appends health objections before unresolved source incidents. A retained closed-market wait can hide a dependent cash/ownership conflict as the first blocker. Prioritize all objection sources together while retaining every objection; add the combined-state regression.

The reviewer ran only the two focused scratch reproductions, not the affected suite or CI again. The [original probe source](review_timing_probes.py) is preserved from `C:/Users/leepa/AppData/Local/Temp/m3-review-83d86309cc3d406092f0bb91d42e6696/test_m3_positive_inventory_latency.py`. Original logs are `paper-core-check-uwxna76o/result.txt` and `paper-core-check-oif49h7h/result.txt`. Two initial external scratch import failures exercised no implementation behavior.

Focused unchanged-code checks covered coordinator send-state transaction (554–607), runtime hook (74–94), certification semantics (store 297–376), and current cash comparison (store 107–116). Surrounding functions were read where diff hunks were incomplete. Root separately verified all 20 frozen source/test hashes, saved log hashes, timeline POST identities/counts, canonical first observation/decision, fill arithmetic, two costed outcomes and repair witnesses using the ignored `verify_m3_trace.py`.

Limits remain explicit: M3's distinct process IDs/epochs simulate separate clock domains inside one Python process. Existing M1/M2 actual subprocess tests have separate attribution. Operational guardian readiness, real adapter/time commissioning, actual PostgreSQL execution and M4 learning are outside this local M3 verdict. No runtime changes or deployment were performed.

## Repair status

The original implementer repaired all three findings in `1730b658f1a67132525abdfaab40547f0a3e624c`. The same independent reviewer returned **ACCEPT; code quality approved** after scoped re-review. No actionable repair finding remains.

## Scoped repair acceptance

- Inventory latency: `trader.py:310` captures a validated projection after synchronization; evaluation uses its timestamp and claim validation reproduces it while separately checking current authority. `time_evidence.py:164` checks age, continuity, process identity and projection consistency. Three positive-latency witnesses each submit once; expired, discontinuous and future evidence still refuses.
- Close-buffer timing: `contracts.py:195` derives a monotonic expiry from the provider deadline and matching latest-provider projection. `time_evidence.py:248` consumes it; `guardian.py:231` handles old plans with retained provider brackets. Both skew directions wait just before, then exit at, conservative expiry. `send_guard.py:110` also checks it inside the existing transaction; the 40-ms wall-movement case is definitively unsent with no POST.
- Blocker priority: `health.py:92` ranks manual pauses, integrity health and dependent incidents before ordinary protocol/retry/wait objections. The combined closed-session/source-conflict witness retains every objection and reports integrity first.

The reviewer checked original-v32 payload/ID compatibility, nine repair witnesses and the repaired eight-stage timeline with five synthetic POSTs and two completed costed outcomes. Frozen affected validation is **424 passed, 26 skipped**, 200.31 seconds, with all six CI gates successful. Earlier 408-pass and initial failing runs remain separate. No repeated reviewer test run was needed.

Root independently verified the 21 final source/test hashes, CI log hashes and nine witnesses directly from the original final log with `verify_m3_fix1_trace.py`. Current projections retain original proof IDs; both skew signs conserve five-share owned exits; old optional fields remain absent; integrity precedes the closed-market wait. [Repair validation](fix1-validation.json), [repair witnesses](fix1-repair-traces.json), [joined timeline](fix1-joined-timeline.json) and [complete log](fix1-green-affected.txt) preserve the evidence.

The integration owner accepts M3's local synthetic capability. The limits above remain; M6 explicitly carries real separate-role timing/restart, independent operational guardian readiness and retry scheduling checks before commissioning. This is not deployment or whole-project acceptance.
