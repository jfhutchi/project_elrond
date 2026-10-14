# M6 repair 2 — PREPARED / WAITING

This focused repair at base `6026f02fd2ca46c541f9e7c45117f4049ce102b0` closes the generation/import fee race. Findings1/2 were independently addressed by fix1; their behavior is retained. No broker probe, operating-store write, task change, commission or deployment occurred. Original M6 and fix1 evidence/indices/manifests remain unchanged.

Every owned contractual-zero event now checks the exact account/contract contradiction under the SAME BEGIN IMMEDIATE transaction as cost import. A contradicted zero is retained in its original immutable raw BrokerEvent source, together with an immutable scoped CONTRACTUAL_ZERO_ADMISSION_REFUSED document referencing that source/cost/fill/client and first conflict. It is omitted from authoritative core_cost_items. Generation before a contradiction cannot bypass admission by importing later. No aggregate fee is allocated, no UNKNOWN amount is invented and no global PAPER stop is added. Existing accepted zeros remain governed by fix1's finite bounded backfill and post-cost/pre-outcome guard. Delayed unadmitted zeros leave actual fees/net unknown and outcomes COSTS_PENDING, so learning excludes them even after pending clears. Replay/restart preserves refusal/raw provenance; unrelated account/contract latches do not block valid costs.

The proposed release is `f0433941ffb9c059b9d7a4b53ab530622968734d1ac50332931180cf4370c5df`, with unchanged config `8c95e278bc674c64557bb0485957d1b6666546e1d67a20e43d30d2079b085c8c`. Typed JSON roundtrip and exact executable inventory verification pass. Prior fix1 release `1e39ceb94155aaa746a879b69306ca3150a182c19d491a139cd81c8a1a58e302` correctly refuses the three changed source pins. No existing operational commission is transferred; executable/owner-generation transfer remains unsupported.

The complete ordered ownership handover and forward-safe recovery are still specified by [the original runbook](../README.md), with [fix1's verified handshake and limitations](../fix1/README.md). Every future command/task proposal must now use the exact fix2 files. The commands below are **unexecuted**, and commissioning/deployment/task operations still require separate explicit approval after scoped re-review and whole-branch review:

```powershell
$Repo = 'C:\Users\leepa\.codex\worktrees\paper-core-m0\project_elrond'
$Evidence = Join-Path $Repo 'docs\evidence\paper-core\m6\fix2'
$Python = Join-Path $Repo '.venv\Scripts\python.exe'
$Config = Join-Path $Evidence 'prepared-config.json'
$Manifest = Join-Path $Evidence 'prepared-release.json'
& $Python -B -m quantbot.paper_core --config $Config --manifest $Manifest --command verify
# Only after separately approved backups, compatibility, token and complete writer fencing:
& $Python -B -m quantbot.paper_core --config $Config --manifest $Manifest --command migrate
& $Python -B -m quantbot.paper_core --config $Config --manifest $Manifest --command acquire --approval-document 'D:\Elrond\review\m6-acquisition-request.approved.json'
& $Python -B -m quantbot.paper_core --config $Config --manifest $Manifest --command commission --approval-document 'D:\Elrond\review\m6-commission.approved.json'
& $Python -B -m quantbot.paper_core --config $Config --command status
& $Python -B -m quantbot.paper_core --config $Config --command export --decision-id 'ACTUAL_DURABLE_DECISION_ID'
& $Python -B -m quantbot.paper_core --config $Config --manifest $Manifest --command fence --approval-document 'D:\Elrond\review\m6-fence.approved.json'
& $Python -B -m quantbot.paper_core --config $Config --manifest $Manifest --command rollback-model --approval-document 'D:\Elrond\review\m6-model-rollback.approved.json'
```

The three exact superseding XML proposals remain disabled, submission-disabled, LeastPrivilege, PT0S and trigger-free. They have not been registered or started. Submission-disabled preflight clamps the same release and does not create another commission; entry-only fencing preserves guardian/outcome risk reduction. Decode XML as explicit UTF-8 text. Current shell identity is not selected leepa Limited task-token evidence. Keep legacy Protection/Deadline and their nine original-owner lots; no adoption, flattening or stop clearing is authorized.

Schema35, immutable historic IDs, account/store binding, owner generation1, initial COLLECT, PAPER_TIME_V2, daily bounded policy and fixed core risk contracts remain unchanged. Learning still requires ten completed sessions, ALL eligible history and fixed 2x latest ten. Proposed first session2026-10-15 must be updated and independently reviewed if handover occurs later. Config contains no component-zero contract and no synthetic learned model is commissioned as real.

Fresh provider/account/current cash/component applicability, canonical metadata, retained-owner provenance, tighter risk controls, actual selected-token/policy/path/job-tree behavior, approved backups/migration/commission/atomic writer fence and natural protected broker-PAPER roundtrip plus later eligible daily activation remain unverified. Fixture labels do not confer broker authority. Date-only fee cash timing and unresolved aggregate allocation remain unknown until proven corrections; no residual rebaseline or zero fallback is permitted.

Preserve schema35 and all later decisions/orders/fills/client IDs during recovery. Never restore an older DB over activity or deploy older code after migration. A scoped refused zero is not a materialized cost revision; current learning excludes its UNKNOWN outcome rather than counting raw source amounts as fees. Read-only export retains the raw source, and the corresponding scoped refusal is preserved under core_provider_acquisitions and in this package's regression exports. No schema/new service/dependency is added.
