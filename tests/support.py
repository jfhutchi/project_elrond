"""Shared helpers for tests that need a kill switch cleared to reach the case under test."""

from __future__ import annotations

from datetime import UTC, datetime

from quantbot.operations.kill_switch import (
    MeasuredReadiness,
    ReadinessEvidence,
    issue_measured_readiness,
)

MEASURED_AT = datetime(2026, 8, 14, 13, 30, tzinfo=UTC)

#: Everything `measure_readiness` probes, for tests that mint readiness directly.
ALL_PROBES = ("BROKER", "ACCOUNT", "RECONCILIATION", "MARKET_DATA", "RISK")


def measured(
    *,
    measured_at: datetime = MEASURED_AT,
    unprobed: tuple[str, ...] = (),
    **changes: bool,
) -> MeasuredReadiness:
    """Readiness for a test that needs trading enabled to reach the thing it is testing.

    Most tests here clear the kill switch as *setup* -- they are about stale data, restart
    recovery or order submission, and a halted switch simply stops them reaching the case. Making
    each of those seed a reconciliation row, a bar and a fake broker just to satisfy a probe would
    add a page of unrelated fixture to every one, and a fixture nobody reads is a place bugs hide.

    So this mints readiness through `issue_measured_readiness`, the same designated issuer
    `measure_readiness` uses. That deliberately means test code can produce readiness that was
    never probed -- and the production guarantee does not rest on it not doing so. It rests on
    `test_only_the_readiness_module_mints_measured_readiness`, which walks `src/` and fails if
    anything other than `readiness.py` calls the issuer. The boundary is enforced where it
    matters rather than by making it awkward everywhere.

    Tests about the *measurement itself* do not use this. They call `measure_readiness` against a
    real database and a fake broker, because there the probing is the thing under test.
    """
    evidence = ReadinessEvidence(
        paper_mode=True,
        account_verified=True,
        broker_healthy=True,
        data_healthy=True,
        risk_healthy=True,
        reconciliation_successful=True,
    ).model_copy(update=changes)
    return issue_measured_readiness(
        evidence=evidence,
        measured_at=measured_at,
        probes=ALL_PROBES,
        unprobed=unprobed,
        detail=(),
    )
