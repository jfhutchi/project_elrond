# Omen PAPER runtime implementation plan

**Goal:** Operate the sole Elrond coordinator on the measured HP Omen, preserving safety and honest evidence.

**Architecture:** Native Windows Task Scheduler supervises separate CLI processes. External configuration fixes PAPER mode, database, locks, reports and strategy; a launcher validates the application parser and clean pinned commit before entering a component. The Pi is retired; other computers are optional future workers only. WSL/Docker are unavailable here; boot/S4U registration returned Access denied, while current-user logon registration succeeded. Signed-in supervision is a partial deployment, never proof of unattended reboot recovery.

**Constraints:** TRADING_MODE=PAPER; LIVE_TRADING_ACKNOWLEDGED=false. Never manufacture broker state or forward history, clear unverified controls, migrate an unavailable ledger, contact the Pi, or merge this branch. Operator authorized routine design/implementation without further approval.

- [x] Fetch origin and isolate from existing checkouts; inventory hardware, processes, startup, state and credential presence.
- [x] Read recovery, configuration, locking, reconciliation, kill-switch and deployment code. Compare native Windows, WSL/systemd and Docker.
- [ ] Add launcher tests that refuse live configuration, missing/ambiguous/relative state, checkout drift, inherited runtime secrets and duplicate component execution. Execute failures before implementing.
- [ ] Implement `quantbot.operations.service` and `deploy/omen/setup-windows.ps1`; delegate supervision to Task Scheduler, preserve CLI safety and use external logs. Verify task registration, process ownership, stop/start and duplicate refusal on this host.
- [x] Remove Pi-specific current recovery blockers without granting global authority; retire its installer and correct topology/deployment guidance. Preserve historical snapshots.
- [x] Explicitly create a fresh PAPER ledger only after bounded recovery scans; record absence of prior local history, keep kill switch engaged, and verify schema/integrity/backup.
- [x] Exercise every requested status/readiness command. Broker-dependent components must truthfully refuse missing credentials. Examine existing local Kronos worker independently of trading.
- [ ] Run focused tests and committed local CI (Windows plus Linux prerequisite result); inspect complete diff and tracked file types, commit and push focused changes, create PR. Record precise runtime acceptance results and remaining access blockers.

**Fresh-state provenance:** No operational ledger identified. Windows.old contained 774 Elrond-schema pytest fixtures, excluded by their test provenance; no operational candidate appeared in D:, OneDrive, Documents, Downloads, both prior recovery directories, ProgramData or Public. Whole-profile scan hit its cap inside packaged worker dependencies, so targeted scans completed the operational locations; unreadable system paths remain explicit. Prior Alienware/Pi evidence is historical and was not accessed or migrated. Broker account is unknown until credentials permit read-only reconciliation; this ledger must not claim a reconciled account or inherited qualification.
