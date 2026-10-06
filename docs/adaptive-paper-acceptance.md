# Adaptive PAPER candidate acceptance

This candidate adds an isolated experiment coordinator, a permanent shared fit budget, point-in-time observations and research journals, retained PAPER execution integration, independent lot ownership, honest forward evaluation, and read-only experiment dashboards. It does not activate a model or change a running service.

## Exact source verification

- Tested and independently reviewed implementation: `d67e072671d5ada299209ef46c701ee77d7c81fc`.
- Tested Git tree: `0146e5fb1138a587140ff7130708628416672b59`.
- Local consolidation: `99b9eb124d2a1c0ed486c767d3846ec102cf199c`, with the identical tested tree. The sole PR258 add/add conflict retained the reviewed function-local import required by the existing architecture guard.
- Frozen aggregate: **3,997 passed, 47 skipped, one dependency deprecation warning**, 931.01 seconds, aggregate exit zero. Ruff, Linux and Windows strict mypy for all 305 source files, and lint/type checks for CI helpers passed. Frozen lock verification and offline dependency sync were reused from the earlier candidate; dependency files remained unchanged.
- Skips cover unavailable WSL/OS sandbox capabilities, optional live model endpoints, disposable PostgreSQL parity, Windows symlink permissions, and environment-specific bootstrap/runtime checks. These capabilities are not claimed verified by the local run.
- An earlier aggregate's failures were investigated rather than waived: native-owner reproduction passed 21 affected provenance/process tests; the architecture import violation was fixed. The final aggregate has no failures. No ownership, global Git trust, or security settings were changed.
- One independent safety reviewer cleared eight findings. Regressions cover cross-root atomic fit reservation, actual queried-date guards, terminal broker recovery without resends, fair bounded historical recovery, news provenance/as-of decisions, coverage-valid review counts, and fresh decisions after recovery latency.

## Review dependency chain

`main` currently ends at the PR257 foundation. Draft PR258, `codex/daily-close-capture-20260930`, includes merged PR259 news research at `4ee6e5ff82355dfb2821f9f187406e661ae875ee`. This branch explicitly incorporates that dependency. Its remaining diff also includes retained five-minute execution, tracking, and compatibility foundations; it must be reviewed as a consolidation, not described as an isolated adaptive-only patch.

Related open compatibility PRs are #260 (console/alerts), #261 (bounded DIA worker), and #262 (learner standby). Their overlapping work is not independently merged or closed by this candidate. Resolve their disposition before a final merge recommendation. Do not merge unrelated dependency chores as part of this rollout.

Hosted CI remains unverified. Reading main branch protection returned HTTP403 with: "Upgrade to GitHub Pro or make this repository public to enable this feature." This does not prove that required protections are absent and grants no permission to bypass them. Existing required-check policy must be established before merge/deployment.

## Evidence boundaries and remaining acceptance

The integrated intent/fill/protective exit/outcome/successor tests use synthetic fixtures. They establish code behavior, not broker profit or prospective learning. Qualified historical archives must remain exploratory and retain actual retrieval availability. Protected windows cannot be recycled merely because their calendar end passed.

The staged launcher is SHADOW-only with trading disabled, a separate experiment store, existing limited task principal, no operational migration, and no protection interruption. Backups exclude credentials; rollback stops only the new task and preserves all later evidence. Charged research commissioning and exact model activation remain separate gates. Existing PAPER entry and protection remain unchanged until an exact authorized transfer.

Before runtime acceptance: establish repository protections, pin the actual release bytes and manifest, verify provider/identity/coverage guards, check the canonical permanent six-fit budget, and obtain exact-artifact activation before any new execution authority. Two additional component alert codes remain disabled externally until the existing route receives the reviewed catalog. Confirm natural coordinator/worker receipts and actual rendered dashboard states after any authorized rollout.

Prospective completion requires a naturally eligible PAPER fill, independently protected exit, reconciled fee-bearing outcome, charged successor, and fresh evaluation. A veto is recorded honestly; no order is manufactured to complete acceptance. Twenty valid forward performance days and the thirty-session insufficiency ceiling remain distinct from historical fit validation and synthetic tests. No automatic live transition or profitability promise is made.
