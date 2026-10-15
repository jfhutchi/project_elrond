# HP Omen deployment staging

**Prepared and staged; functional activation is waiting for an existing broker order.** This follows the user's request to deploy on the current HP Omen, remediate Mako, and enable local PowerShell scripts persistently. No operating database has been migrated and no replacement trading role has been started.

## Current release and corrected account binding

The current prepared release is `ec15cbd8dc28a1f90adcb03b26669552eb94b7fba4fa6e208c7cf5d342baf645`; config `aa6c93bccdf4f28a5c1cfda1d94efd1ace09bc1c7e5fd10da3e6298cb68679e2`; schema 36. It retains the reviewed application and patched Mako 1.4.3 environment from [the security update](../mako-security/README.md).

The earlier preparation copied the legacy account digest, `content_id({'paper_account': account_id})`, into a field whose runtime contract requires plain SHA256 of the account ID. Read-only comparison confirmed the configured account and stored operational account are identical; a subsequent GET-only PAPER broker check confirmed the same broker account. The prior `derived_core` check correctly refused the old digest; the corrected configuration passes for that same account. No account was changed and no identity guard was bypassed. `account-binding-correction.json` records only digests and outcomes.

All application, launcher, interpreter and installed-distribution pins are identical to the Mako security package. Only `expected_account_hash` changes in the runtime configuration. The trading/learning policy, store identity, legacy owners, learning origin and limits are unchanged. Historical manifests retain their original bytes.

## Backups and migration rehearsal

Fresh SQLite online backups include WAL contents for the operational, learner and experiment databases. Each backup has an identical separate restored copy with `integrity_check=ok`. Snapshots are individually consistent, not an atomic cross-store transaction. Their locations, timestamps, sizes and hashes are in `backup-rehearsal.json`.

Only additional operational and learner copies were migrated from schema 29 to 36. Both pass integrity checks and preserve the store identity plus every original value in all 59 original data tables, excluding only the intentionally updated schema/Alembic markers. Added columns/tables are allowed; the historical numeric fee columns are unchanged. Per-table fingerprints are retained beside the host backups. No live migration was performed.

The operating snapshot has 17 FILLED intents, one REJECTED intent and one BROKER_ACCEPTED intent. The durable manual stop is disengaged and was not changed. Nine inherited positions remain assigned to `adaptive-momentum-v1-309894d8d8a5296e`. The dated last protection evaluation was HEALTHY; it is not represented as a fresh broker protection proof.

## Concrete activation blocker

The read-only broker check at **2026-10-15 01:44:15 UTC** reports an outstanding **IWM SELL of 0.169669 shares**, zero filled, status `accepted`, submitted at **2026-10-14 20:05:02 UTC**. Its original client and broker IDs agree with the retained local intent. The market is closed; the broker reports the next opening at **October 15, 09:30 America/New_York**. This is an existing exit, not an order created by this deployment.

The approved commissioning contract requires no open orders. The exit was not cancelled, replaced or resubmitted, and its pending state was not rewritten. Existing Protection and Paper Deadline services continue to own their responsibilities. The new guardian/outcomes/trader tasks are registered disabled, with submission and entry clamps and no triggers. They have not run or acquired trading authority.

When the original exit becomes terminal, first reconcile it through its original IDs and refresh ownership, quantities, task/process writers and backups. Fence old entry writers while preserving existing exits; then perform the reviewed additive operating migration and fresh bounded acquisition/commission. Acquisition and commissioning must use this corrected release/config, the original freshness windows, and newly observed evidence. Prior snapshots or timestamps cannot authorize a later handover. Independent guardian/outcomes readiness precedes enabling the new trader. No natural PAPER lifecycle or daily learning activation is claimed.

The user's PowerShell request has been applied as **Windows PowerShell CurrentUser RemoteSigned** under the actual `leepa / Interactive / Limited` task identity. No machine/group-policy setting was changed and no Bypass argument was added. Existing functional task definitions remain unchanged. The verifier-only task is disabled again after its exact-release check; its result and log are captured with this package.
