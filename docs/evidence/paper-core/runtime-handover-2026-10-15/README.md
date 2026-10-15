# Omen runtime handover — October 15, 2026

Measured at 08:13 Eastern (12:13 UTC): the merged stack and locked dependencies are installed, both operating and learner stores are migrated to schema 36, and the upgraded dashboard, inherited-position protection and deadline services are running. The core trader is **not commissioned or trading yet**. The separate one-time deployment task is running and waiting for the original IWM exit. This dated snapshot does not claim a future successful activation.

- Runtime source: merged PR 290, `14352cc428dcd999750138d52fbd5f30347991a1`.
- Core release: `0734974b39088b449abe1279a385c89202756b61f3420aa910d6b2d772a46d4c`; config `620d31ec42915eee76779a972521428adaeb9c0a5536694422d8c27e2b6acebd`. Direct verification at 08:07 Eastern still has zero objections. The [earlier installation](../local-deployment-2026-10-15/README.md) preserves all 366 executable pins and 46 distributions, including Mako 1.4.3.
- Installed core: `D:/Elrond/releases/paper-core-14352cc-20261015`. Compatible inherited services: clean native Git checkout `D:/Elrond/releases/legacy-support-14352cc-20261015`, same merged source and locked dependencies. Original checkouts/environments are retained.
- Dashboard: <http://127.0.0.1:8787>. Root, API state, kill-switch page and learner API returned HTTP200 in [the capture](handover-capture.json).

## Stores and service handover

All 19 legacy tasks were disabled and all old QuantBot process trees were verified absent. Fresh WAL-aware backups and restored copies passed integrity/hash checks. Operational and learner 29 → 36 migration preserved each store identity and every original column/row across 59 data tables per store. Manual stop remains disengaged with its original timestamp/reason. The experiment journal was backed up and retained unchanged. See [migration](operating-migration.json), [rehearsal](backup-rehearsal.json) and [writer fence](writer-fence.json). Backups remain under `D:/Elrond/review/paper-core-activation-20261015T1155Z/fenced-backups`; do not overwrite a subsequently active36 store with29 data.

Three retained services now run the merged 14352cc code: Elrond Protection, Elrond Paper Deadline and Elrond Console. They retain their existing login/recovery triggers. The other 16 legacy tasks remain disabled, including obsolete entry/learner/research producers and the unused model server. The new core replaces their entry/daily-learning path. The four original core tasks remain disabled at this capture.

The fresh no-submission inherited protection assessment is **DEGRADED / POSITION_STATE_UNRECONCILED**, with nine positions and nine unprotected by that assessment. It must not be represented as current healthy protection. The new protection contract requires a core-certified inventory, which cannot exist until the new guardian runs after commissioning. Processes alone are not proof of protection. Entries remain disabled until a fresh post-guardian assessment succeeds.

## Pending original exit and bounded continuation

At08:13 Eastern the broker still has the original IWM MARKET DAY SELL0.169669 accepted/unfilled. Its client ID is `qb-595f587acd1b769f4bd0d59282e631ad14ecb9f7`, broker ID `28523181-cd04-4f74-bd67-bcebe4582164`. All nine inherited holdings keep owner `adaptive-momentum-v1-309894d8d8a5296e`. No order was cancelled/replaced and no position was adopted, liquidated or reset by this handover. Broker next open is 09:30 Eastern.

`Elrond Paper Core Deploy` is a running one-time native task under leepa / Interactive / Limited. It polls real read-only broker state, requires the original order's genuine terminal state plus matching durable reconciliation, zero open orders, unchanged inherited quantities/owners, an unchanged verified release/config, fenced old writers and no explicit manual pause. It then invokes the unchanged supported fresh acquire/commission paths. Initial commissioning is bounded to this first session before 10:00 Eastern; expiry or changed provenance refuses activation. Network failures retry within that window. It does not cancel orders, invent fills/cash/fees, clear stops or bypass commissioning guards.

After commissioning it starts independent guardian and outcomes roles with entries disabled, requires two advancing successful polls and a fresh healthy inherited protection/deadline assessment, then starts trader. Functional core tasks receive login and five-minute recovery triggers. A failed readiness check leaves trader disabled. Success writes an actual local completion record and disables the one-time deployment task. No completion record exists at this capture.

The [actual selected-token check](continuation-selected-token-check.json) passed by returning expected wait code 10 for the original pending order. Thirteen pure deployment-gate tests pass, and fresh review's two findings were corrected: stale progress cannot certify readiness and the assessment's GET-only requests/lock are bounded. These checks do not prove the future open-market activation path has completed.

Live local status: `D:/Elrond/review/paper-core-activation-20261015T1155Z/activation-state.json`; task log: `D:/Elrond/logs/paper-core-deployment-launcher.log`. Later success/refusal appears respectively in `activation-complete.json` or `continuation-failure.json` beside the state file. Private broker acquisition, account identity, environment contents and all credential values are excluded from this package. No paid CI was triggered.
