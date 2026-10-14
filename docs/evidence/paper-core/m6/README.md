# M6 preparation: PREPARED / WAITING

This package is a reviewable runtime release, not an operational commission. The operating stores, broker, tasks and production processes were not changed or probed. Broker state is **NOT_PROBED**. Operational deployment and natural broker-PAPER acceptance remain **WAITING**.

The frozen executable identity is `0c55ea726d4bfe6cf390379789d96527e89450f3b24a8c286e9b147926d08f9e` in `prepared-release.json`, derived from exact executable inventory, dependency files and installed distribution bytes, interpreter/base runtime, schema35 and `prepared-config.json`. Config identity is `8c95e278bc674c64557bb0485957d1b6666546e1d67a20e43d30d2079b085c8c`. The unbound input release field is mandatory; startup derives the bound core config from the manifest and exact existing account identity/hash. This avoids a recursive hash. No lane string, placeholder account, config file or fixture authority can commission PAPER.

Initial model is explicit nonpredictive COLLECT `8a160f52b4086ba3f62269b2fb653c635fc45abc5acdc1a32b85a2c1aa77ac01`. The proposal chooses DAILY_BOUNDED_V2: after ten completed exchange sessions, daily consideration of ALL eligible history with fixed 2x latest-ten-session weight. This policy requires explicit approval of this exact release at commissioning. It does not imply that a human reviewed each candidate. NO_CHANGE and transient retry remain honest. The proposed learning origin is 2026-10-15T13:30:00Z; if handover occurs later, regenerate and independently review the config/release with the actual first session. Never count precommissioning sessions or activate a synthetic learned model.

Fixed risk remains one core position, 50bps per new position, 25bps entry risk, 100bps stop, 30-minute horizon, 120-second close buffer and 500bps buying-power reserve. Current inherited exposure participates in safety checks; it is not assigned core ownership. Optional research/model-server/roster/dashboard/general-veto policies are excluded explicitly. Settings PAPER/account authority, market eligibility, current inventory/cash, stop state, unknown submissions, ownership, guardian readiness and final-send authority remain enforced.

## Source and executable interfaces

`python -B -m quantbot.paper_core` supplies trader, guardian and outcomes as independent processes. `deploy/omen/start-paper-core.ps1` uses the existing virtual environment, hidden job host and supported execution policy. It never installs packages or applies ExecutionPolicy Bypass. The global Alpaca adapter, ExecutionCoordinator and Settings callers are unchanged. The narrow core broker composes the retained retail PAPER transport, clock/calendar/market clients and paginated activities. The runtime-only coordinator overrides `_gate` and `_adopt`; `_adopt` reloads current immutable intent under BEGIN IMMEDIATE, retaining client identity and existing acknowledgement/terminal conflict checks. The parent claim, unknown reservation, reduce-only exit and transactional final-send machinery remain.

The core gate retains validate_trading_authority, exact PAPER/expected account, manual stop and KILL entry restrictions, actual current account/positions, fixed sizing and reserve controls and market eligibility. STRATEGY_EXIT additionally requires original owned plan and remaining owned quantity. DRAWDOWN_LIQUIDATION is unsupported and refused. A manual entry stop does not prevent risk-reducing owned exits. The trader stages the plan and original intent; only an independently identified guardian can acknowledge it. Both actual locked-send and runtime authority check the durable typed commission. Entry cannot acknowledge its own guardian, import synthetic cash/health or claim inherited lots.

Guardian/outcomes poll at most every15seconds independently of bar waits and asynchronous fitting. Retries cap at15seconds with bounded jitter. Heartbeats include actual PID/monotonic epoch, useful progress, proof age, time-proof age, duration and actual first claim/send blocker. Guardian reads use indexed current role pointers; historical documents remain immutable. Runtime commissioning/fence events are sparse and their lookup remains linear in that sparse history, a minor review limitation.

Outcome imports use a durable bounded cursor with two-day overlap and at most six-hour advance, unresolved/recent revisions, original client IDs and stable order/fill watermark checks. Retained-owner fills update a separate external accounting ledger, retaining their original owner provenance without granting core exit ownership. Current cash and all-in fees are separate proofs. Date-only activity boundaries do not certify cash timing. Late fee/component conflicts produce revised UNKNOWN outcomes with raw provider provenance; empty fees never imply zero. Component-specific documentation is only accepted through an exact account/endpoint/symbol/effective contract; all five components are required for all-in zero. This package supplies no such operational contract.

Canonical learning authority uses metadata-only read-only/query_only snapshots of the current learner source, retaining canonical `us-equities-daily` separately from owned cohort `ALPACA_PAPER_OWNED`. It refreshes immediately before materialization/fit and candidate activation. Current holdouts/quarantines invalidate older eligibility; source unavailable/stale blocks learning with retry/NO_CHANGE, without becoming an unrelated entry or guardian dependency. Imported permanent trial identities/counts are preserved without charging copies. No feature/target payloads were read from operating stores.

## Dated continuity and ownership

`preparation/provenance.json` pins the root's33 nonsecret artifacts: WAL-aware online backup/restore/full integrity reports for operational, learner and experiment stores, host/inventory/metadata, two source JSON configs,19 task XMLs, remote refs, v34 root verification and canonical registry metadata. These are dated preparation, separately captured and NOT cross-store atomic. Archived XML bytes are UTF-8 although declarations retain UTF-16: decode UTF-8 text before parsing or future Register-ScheduledTask -Xml. Do not edit original archive bytes.

At2026-10-14T17:17:03Z the dated legacy protection report shows9 healthy protected positions: DBC,EEM,EFA,IWM,QQQ,SPY,XLE,XLK,XLV. Keep `adaptive-momentum-v1-309894d8d8a5296e` v1.2.0, config `309894d8d8a5296edd29a713cf1cbad8df84c4dc46cae5472e802baddc810c51`, policy position-protection-v2 hash `c83578519cce7061079c8dfc4ddc518569ba21f4be9cf46465004066afa1f5ad`. **Elrond Protection and Elrond Paper Deadline must retain these lots** until a separately tested explicit transfer. No lot adoption or forced flattening is proposed. If retained-owner activity cannot be proved from original durable intent/owner provenance, current cash/inventory remains blocked; acquire that metadata and replay the same external event, never balance an account residual or rebaseline over activity.

All18 trading/protection/learner tasks use leepa/Interactive/Limited. Only optional Elrond Model Server uses erlond/Password/Limited, and it is not a core dependency. Current shell username does not prove task-token behavior. The archived learner action contains ExecutionPolicy Bypass; the new proposed actions deliberately omit it, leaving supported-policy startup an explicit unverified prerequisite.

`task-proposals.json` pins three disabled, submission-disabled task XML proposals: Elrond Paper Core Guardian, Elrond Paper Core Outcomes, Elrond Paper Core Trader. They reuse the archived Limited leepa principal and existing hidden service host `D:\Elrond\state\service-host\1188bba2cb38de299aad0768\elrond-service-host.exe`. They have no recurring trigger, role-specific URI/description, explicit LeastPrivilege and ExecutionTimeLimit PT0S. Continuous guardian/outcome protection must not inherit the old learner PT5M termination. task-proposal-verification.json proves these exact XML values/hashes; this is static verification only. They were generated locally and were NOT registered, enabled or started. Future scheduling/recovery trigger selection remains part of the reviewed task handover.

## Future commands: require separate rollout approval

These are concrete proposed commands, **not executed**. `$Repo` and config/manifest paths below match the proposed release; copying to another directory changes config/release identity and requires prepare+review again. Use the existing Python3.11.16 and installed locked dependencies, never startup downloads.

```powershell
$Repo = 'C:\Users\leepa\.codex\worktrees\paper-core-m0\project_elrond'
$Evidence = Join-Path $Repo 'docs\evidence\paper-core\m6'
$Python = Join-Path $Repo '.venv\Scripts\python.exe'
$Config = Join-Path $Evidence 'prepared-config.json'
$Manifest = Join-Path $Evidence 'prepared-release.json'
& $Python -B -m quantbot.paper_core --config $Config --manifest $Manifest --command verify
```

1. Independent M6 review then whole-branch review must accept exact executable/config/runtime pins. Explicit rollout approval must name initial COLLECT, daily policy, owner generation1, actual first learning session, deployment directory, all current tighter risk controls and task proposal changes. No account credentials are added. Refresh WAL-aware backups/restore verification and current task/process/owner metadata. Preserve source store UUID `cd2a5911-853a-484e-bbc8-d0c27b5d27aa` and stop attribution; dated schema29 is not schema35. The dated entry receipt was OBSERVING/FORWARD_EVIDENCE_ACCUMULATING; the earlier mapper UNRECOGNIZED_REASON is not a new integrity stop. Do not clear any existing stop.
2. Under separate approved task/token testing, verify the exact release using the selected leepa Limited task token. Prove path, host/job-tree, stdout/log permissions and supported policy; no elevation, bypass, security rewrite or new service. A precommission role startup correctly refuses CORE_RUNTIME_UNCOMMISSIONED. After commissioning, selected-token role preflight uses the SAME manifest with `-SubmissionDisabled`; disable-only clamps never enable a config disabled in the manifest.
3. Identify and fence ALL legacy new-entry writers and wait for verified task-owned process trees and original pending intent recovery. Archive exact final task definitions first. Candidates from dated metadata include Elrond Daemon, Elrond PAPER Exploration, Elrond Adaptive Paper Shadow and Elrond Learner; verify current actual writer actions before naming an approved stop set. Other research/observation/learning consumers may be retired only after source dependencies are proved. Keep Protection/Deadline and their legacy owner untouched. A Stop-ScheduledTask alone is not proof that broker submission cannot race. No old/new entry generations may coexist.
4. On approved new restore copies, verify migration/recovery before original migration. Exact approved original command (only after backup and compatibility acceptance):

```powershell
& $Python -B -m quantbot.paper_core --config $Config --manifest $Manifest --command migrate
```

5. Refresh current broker/account/positions/open-orders, clock/calendar, canonical authority and exact retained-owner provenance via separately approved read-only acquisition. Fill `acquisition-request.template.json` with real approval and verified writer-fence evidence, leaving no placeholder accepted as operational evidence. It requires no pending orders and refuses unresolved original submission. Do NOT use dated root snapshots as fresh authority.

```powershell
$Request = 'D:\Elrond\review\m6-acquisition-request.approved.json'
$Commission = 'D:\Elrond\review\m6-commission.approved.json'
& $Python -B -m quantbot.paper_core --config $Config --manifest $Manifest --command acquire --approval-document $Request
# Save exact nonsecret returned draft, verify source hashes and obtain explicit approval.
& $Python -B -m quantbot.paper_core --config $Config --manifest $Manifest --command commission --approval-document $Commission
```

Acquisition and commission must fit the120second freshness window; canonical source/account/retained positions must be stable. Review fresh tighter risk constraints and cost/cash component applicability before approval. No balancing cash residual is valid proof. Commission is explicit and startup never performs it. Config hash is derived from real expected account and manifest, not the config placeholder. Initial model is separately bootstrapped by this explicit command; interrupted commission/bootstrap needs repair under review, not a repeated commission. Historical store config binding is never overwritten silently: this initial operational commission requires a store without an existing core baseline/commission. An accepted populated synthetic v34 store is a migration compatibility fixture, never an operational commission target.

6. Register/start only the exact independently reviewed task XMLs after approval. Proposed definition import must decode text explicitly:

```powershell
$XmlPath = Join-Path $Evidence 'task-proposal-guardian.xml'
$XmlText = [IO.File]::ReadAllText($XmlPath, [Text.Encoding]::UTF8)
Register-ScheduledTask -TaskName 'Elrond Paper Core Guardian' -Xml $XmlText
# Repeat exact reviewed Outcomes/Trader definitions; enable/start only when explicitly approved.
```

Start submission-disabled guardian/outcomes before trader. Verify distinct PID/epoch, actual useful progress/proof age, same release/store/owner/config, source receipt and exact current blocker. Stop the reviewed preflight instances and remove only the disable-only submission flag in the approved task action before enabling submissions; the manifest/config remains EXACTLY the same. Preserve current stop. Start guardian and outcomes before trader and require their independently fresh readiness. The legacy XLE position already blocks a new XLE entry until naturally gone; no forced liquidation. Market-open evaluation is not submission acceptance.

7. Natural broker-PAPER acceptance later requires an unforced eligible decision with joined exact observation/model/activation/real learning receipt, original client order/intent, actual broker fills, owned protection/exit, terminal reconciliation and complete cost/outcome revisions. Initial COLLECT has no fitted learning receipt; warming up and missing cost/cash proof remain WAITING. Actual daily model eligibility/activation and later decision attribution must be observed separately. Never claim natural PAPER or profitability from the isolated witnesses.

Read-only supported inspection (no CoreStore constructor/migration):

```powershell
& $Python -B -m quantbot.paper_core --config $Config --command status
& $Python -B -m quantbot.paper_core --config $Config --command export --decision-id 'ACTUAL_DURABLE_DECISION_ID'
```

## Forward-safe recovery

After any schema35 events, do not point M5/schema34 code at the store and do not restore a prehandover DB over newer decisions/orders/fills. Schema35 downgrade is explicitly unsupported. Old plans retain immutable original owner/config/client identity; release/owner transition to a later executable generation is refused until separately implemented/reviewed. This package supports initial generation and forward recovery under its pinned runtime, not silent config overwrite. No historical payload is rewritten.

For ordinary clock/provider/canonical acquisition faults, bounded role retry and fresh evidence recovery preserve guardian/outcomes; no automatic global PAPER kill or manual reset. Entry can be fenced explicitly while owned risk reduction remains available. Approved recovery command:

```powershell
$Fence = 'D:\Elrond\review\m6-fence.approved.json'
& $Python -B -m quantbot.paper_core --config $Config --manifest $Manifest --command fence --approval-document $Fence
```

Fence document contains the exact recorded `commission`, a concrete `reason`, and nonempty actual `evidence_ids`. Keep guardian and outcomes running WITHOUT `--submission-disabled`; use `--entry-disabled` if restarting roles for forward recovery. Recover unresolved original client orders before any new claim. A fenced entry cannot resume through startup or a placeholder document; a later independently reviewed resume procedure is required. Sparse fence history remains immutable.

For model-only recovery, exact rollback document contains current recorded `commission`, real `approval_reference`, `expected_model_id`, `expected_activation_id`, already reviewed ancestor `target_model_id`, and `reason`:

```powershell
$Rollback = 'D:\Elrond\review\m6-model-rollback.approved.json'
& $Python -B -m quantbot.paper_core --config $Config --manifest $Manifest --command rollback-model --approval-document $Rollback
```

Rollback atomically appends a new activation generation at a reconciled claim boundary, verifies current model/event, reviewed ancestor and feature contract, and preserves all fills/protection/receipts. It cannot adopt old-generation positions or restore a synthetic learned model as real. If no reviewed compatible ancestor exists, keep entry fenced and repair forward. Archived old task XMLs are evidence for reviewed recovery, not permission to blindly restart overlapping old entry writers.

## Explicit unverified operational steps

Fresh broker/account/clock/calendar and retained-owner provenance; actual PAPER nontrade timing/current cash and all-component cost applicability; current canonical authority refresh; any tighter configured risk controls; selected Limited task-token/host/policy/path/job-tree behavior; approved backup refresh/migration/commission and atomic writer handover; natural eligible entry/protected exit/complete costs; ten actual completed sessions and daily learned activation. Current9 legacy positions and their existing owner remain untouched. These gaps require actual approved verification. The mock PAPER commission and local process witnesses do not satisfy them.

Provider sources used for preparation (public documentation, no authenticated verification): [PAPER simulation applicability](https://docs.alpaca.markets/us/docs/paper-trading), [activity schemas](https://docs.alpaca.markets/us/docs/account-activities), [activity date/creation filtering](https://docs.alpaca.markets/us/reference/getaccountactivitiesbyactivitytype-1), [regulatory fee timing](https://docs.alpaca.markets/us/docs/regulatory-fees). The PAPER page's component exclusions do not establish all-in fee completeness or this account's actual cash behavior. Late activity and precise timing remain separate requirements.

