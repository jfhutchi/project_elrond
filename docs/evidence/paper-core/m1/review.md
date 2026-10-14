# M1 independent review

Reviewer: `/root/m1_review` (fresh context, GPT-6 Astra). Base `3e655b63a4df702b9cc193a6cd41af19f195e354`; reviewed head `8ccdb39d6e54b97776944c67f4fe758fadc852e8`.

## Initial verdict: REWORK

One P1 finding: `paper_core/trader.py:216` submits through the bare coordinator after the fixed caller-time check at line 190. The final awaited account/position fetch can outlive the decision, protection deadline or guardian receipt. A credential-stripped synthetic probe advanced provider time on the second account fetch and observed a BUY at 14:31, although the decision expired at 14:02 and the protection deadline was 14:30. This is an M1 pre-send boundary defect, independent of M3 timing-contract work. Fix with an explicit fresh-clock final-send check of durable decision, plan and receipt, including exact expiry and delayed-acquisition tests.

Confirmed red probe: `C:\Users\leepa\AppData\Local\Temp\paper-core-m1-review-519526f08f5c488dba69486b514f9095\test_m1_review_probe.py`; log `C:\Users\leepa\AppData\Local\Temp\paper-core-check-1qrbkzhr\result.txt`.

No other important/critical findings or actionable minor findings. A suspected same-timestamp certified-snapshot selection issue did not reproduce in two targeted probes and was not reported as a defect.

## Integrated evidence assessment

The saved immediate synthetic trace is valid, but M1 is not accepted until the finding is repaired. Independent checks verified typed observation/decision/config/model identities, proof references, protection acknowledgement before entry, canonical fill IDs and terminal orders, five owned XLE shares in/out, two inherited SPY shares preserved, generation-4 owned-flat proof, cash conservation, gross P&L zero and `COSTS_PENDING` with null net. Observation hashing requires its typed contract because exported JSON uses `Z` while canonical model hashing uses `+00:00`; the typed digest matches.

Passing independent trace probe: `C:\Users\leepa\AppData\Local\Temp\paper-core-check-xbx_g0sw\result.txt`.

Reviewed test assertions also support atomic claim/protection reservation, duplicate suppression, manual-pause checks, separate-process guardian exit, non-$100 basis, additive migration and repeated raw-mismatch refusal with both certified and uncertified starting states. The reviewer did not rerun suites or change repository/runtime/operational state. Root owns broad regression. Costs, partial/unknown/restart recovery, full timing, learning and commissioning remain later milestones.

## Resolution

**ACCEPT after fix round 1**, independently reviewed from `8ccdb39` to `d24a7f45cf74c1b0079922172379090814634148`. No material new defects found.

The coordinator invokes the synchronous guard after all awaited acquisition and before SUBMITTING/POST. The guard reloads durable authority, selects the latest matching guardian acknowledgement, and samples current synthetic time after acquiring the writer lock. All entry expiry boundaries are exclusive. A recorded refusal survives a crash before ERROR and prevents clock-reversed replay. Slot release requires the exact definitive-unsent refusal, ERROR and no broker order; the retained irreversible lifecycle prevents treating a prior SUBMITTING attempt as unsent. Unknown/submitting/protocol-error reservations remain intact, and owned exits bypass entry expiry.

The reviewer independently joined [rework-refusal-recovery.json](rework-refusal-recovery.json): old 14:00 intent, durable 14:31 refusal, ERROR, no old broker order; fresh 14:31 observation with separate decision/protection links and the sole filled five-share order, ending cash $99,500. Original immediate round-trip evidence remains accepted.

All 11 broad failures are resolved in the [325-pass combined run](rework-green.txt), preserving original attribution and protection assertions. Default fixture pause is retained; legacy ownership fixtures independently assert one share, $100 basis, $899.90 cash and the original −$10.30 outcome. Additive migration checks still reject malformed tables and preserve the v29 marker. Full Ruff and both-platform source/helper type checks pass. The reviewer inspected these results and tests without rerunning suites or changing files.

Root accepts M1 for its synthetic integrated capability and the source repair. Costs, broader partial/uncertain/restart recovery, operational timing, learning and deployment remain later work. The initial broad run and targeted repair run are reported separately; this is not one wholly green final-commit full-suite invocation.
