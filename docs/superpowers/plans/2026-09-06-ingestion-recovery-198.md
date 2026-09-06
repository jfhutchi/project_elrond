# Ingestion Recovery Implementation Plan

> Execute inline using the executing-plans workflow; request independent review before completion.

**Goal:** Make the daily collector's durable history and checkpoint agree with the data it stored.

**Architecture:** Extend the existing MarketDataSync and IngestionControlPlane. Reuse provider
retries, calendar semantics, validation, and immutable bar storage. No schema or deployment change.

**Tech stack:** Python 3.11, asyncio, SQLAlchemy, SQLite, pytest.

- [x] Inspect #198 and comments, current collector, control plane, cache and provider retries.
- [x] Verify baseline: ingestion-control and runtime-wiring tests (29 passed).
- [ ] Add regression tests in tests/integration/test_ingestion_recovery.py and observe failures.
- [ ] Capture pre-fetch cursor versions, record query ranges/modes, bound collection duration,
      extend recovery windows from durable state, and refuse incomplete successful checkpoints.
- [ ] Verify replay, duplicate handling, disabled sources, timeouts, retries and concurrency.
- [ ] Update docs/ingestion-control.md with behavior, default deadline and operational limitations.
- [ ] Run Ruff, both mypy platform targets, and the full isolated test suite; review and fix findings.
- [ ] Commit the change and create a draft PR linked to #198 with measured verification results.
