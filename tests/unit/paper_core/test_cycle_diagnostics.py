"""Observational trader cycle explanations must never become trading authority."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from tests.integration.paper_core.test_daily_selection import NOW, cohort, prepare

from quantbot.paper_core import roles


@pytest.mark.asyncio
async def test_empty_cohort_reasons_and_evidence_survive_intervening_poll(
    tmp_path, monkeypatch, capsys
):
    db, store, broker, *_ = prepare(tmp_path)
    captured = cohort(
        unavailable=tuple((s, "FRESH_COMPLETE_BAR_UNAVAILABLE") for s in ("AAPL", "MSFT", "NVDA"))
    )
    clock = SimpleNamespace(now=NOW, process_id="cycle-observation")
    clock.sample = lambda: (clock.now, 100.0)
    store.clocks = clock

    async def acquire():
        return captured

    async def empty_batch(*args, **kwargs):
        # The real batch records this before its empty return. Preserve its identity here.
        from quantbot.paper_core.selection import record_cohort

        record_cohort(store, captured)
        return ()

    async def advance(seconds):
        clock.now += timedelta(seconds=15)

    # Isolate role plumbing from admission-policy commissioning, never broker access.
    monkeypatch.setattr(type(store), "parallel_entries", property(lambda self: True))
    monkeypatch.setattr(roles, "trader_batch_cycle", empty_batch)
    monkeypatch.setattr(roles.asyncio, "sleep", advance)
    broker.observation_cohort = acquire
    runtime = SimpleNamespace(
        entry_poll_seconds=15, guardian_poll_seconds=15, outcomes_poll_seconds=15
    )
    try:
        await roles.run_role(store, broker, runtime, "trader", cycles=2)
        import json

        cycles = [
            json.loads(line)
            for line in capsys.readouterr().out.splitlines()
            if '"PAPER_TRADER_CYCLE"' in line
        ]
        assert len(cycles) == 2
        assert "DATA_UNAVAILABLE" in cycles[0]["outcomes"]
        assert "AWAITING_EVALUATION_BOUNDARY" in cycles[1]["outcomes"]
        assert "DATA_UNAVAILABLE" in cycles[1]["outcomes"]
        assert cycles[0]["evaluation_evidence_ids"] == cycles[1]["evaluation_evidence_ids"]
        assert cycles[0]["evaluation_evidence_ids"]
        assert cycles[1]["last_observed_work_at"] is None
        assert cycles[0]["cycle_id"] != cycles[1]["cycle_id"]
    finally:
        db.close()


def test_submission_request_is_not_acknowledgement_or_fill():
    from quantbot.paper_core.cycle_diagnostics import TraderCycleDiagnostics

    observed = TraderCycleDiagnostics()
    observed.submission_requests = 1
    result = observed.snapshot(cycle_id="cycle", now=NOW, waiting=True, reasons=(), failure=False)
    assert "SUBMISSION_REQUESTED" in result["outcomes"]
    assert "EXECUTION_ACKNOWLEDGED" not in result["outcomes"]
    assert "FILL_CONFIRMED" not in result["outcomes"]


def test_evaluation_distinguishes_exclusion_data_and_market_close():
    from quantbot.paper_core.cycle_diagnostics import TraderCycleDiagnostics

    observed = TraderCycleDiagnostics()
    captured = cohort(
        unavailable=(("MSFT", "ASSET_NOT_FRACTIONABLE"), ("NVDA", "FRESH_COMPLETE_BAR_UNAVAILABLE"))
    )
    decision = SimpleNamespace(
        id="decision", gates=("ENTRY_RISK_LIMIT",), action="WAIT", selection=None
    )
    observed.evaluate(captured, (decision,), evidence_ids=("cohort", "decision"), now=NOW)
    result = observed.snapshot(
        cycle_id="cycle",
        now=captured.session_close,
        waiting=False,
        reasons=("CORE_INDEPENDENT_GUARDIAN_NOT_READY", "private provider detail"),
        failure=False,
    )
    assert {
        "MARKET_CLOSED",
        "DATA_UNAVAILABLE",
        "CANDIDATES_EXCLUDED",
        "RISK_OR_AUTHORITY_BLOCKED",
        "AWAITING_GUARDIAN",
    } <= set(result["outcomes"])
    assert "private provider detail" not in str(result)
    assert result["evaluation_evidence_ids"] == ("cohort", "decision")


@pytest.mark.asyncio
async def test_nonparallel_empty_cohort_failure_keeps_actual_unavailable_evidence(tmp_path, capsys):
    import json

    db, store, broker, *_ = prepare(tmp_path)
    captured = cohort(
        unavailable=tuple((s, "FRESH_COMPLETE_BAR_UNAVAILABLE") for s in ("AAPL", "MSFT", "NVDA"))
    )
    store.clocks = SimpleNamespace(process_id="empty-cycle", sample=lambda: (NOW, 100.0))

    async def acquire():
        return captured

    broker.observation_cohort = acquire
    runtime = SimpleNamespace(
        entry_poll_seconds=15, guardian_poll_seconds=15, outcomes_poll_seconds=15
    )
    try:
        await roles.run_role(store, broker, runtime, "trader", cycles=1)
        events = [
            json.loads(line)
            for line in capsys.readouterr().out.splitlines()
            if '"PAPER_TRADER_CYCLE"' in line
        ]
        assert "DATA_UNAVAILABLE" in events[0]["outcomes"]
        assert "OPERATIONAL_FAILURE" in events[0]["outcomes"]
        assert events[0]["evaluation_evidence_ids"]
    finally:
        db.close()


@pytest.mark.parametrize("filled_quantity, expected_fills", [(0, 0), (1, 1)])
def test_only_positive_broker_fill_quantity_confirms_fill(filled_quantity, expected_fills):
    from quantbot.paper_core.cycle_diagnostics import TraderCycleDiagnostics

    observed = TraderCycleDiagnostics()
    observed.acknowledge(
        SimpleNamespace(broker_order=SimpleNamespace(filled_quantity=filled_quantity)), now=NOW
    )
    assert observed.acknowledgements == 1
    assert observed.confirmed_fills == expected_fills


@pytest.mark.parametrize(
    "private",
    ["PRIVATE_TOKEN_ABC123", "provider secret prose", "PRIVATE_TOKEN_ABC123:provider secret"],
)
def test_unknown_reason_text_is_replaced_by_generic_code(private):
    from quantbot.paper_core.cycle_diagnostics import reason_codes

    assert reason_codes((private,)) == ("UNCLASSIFIED_REASON",)
    assert reason_codes(("CORE_MANUAL_ENTRY_PAUSE:private suffix",)) == ("CORE_MANUAL_ENTRY_PAUSE",)


@pytest.mark.asyncio
@pytest.mark.parametrize("hook", ["evaluate", "snapshot"])
async def test_diagnostic_hook_failure_preserves_decision_and_heartbeat(
    tmp_path, monkeypatch, capsys, hook
):
    import json

    from quantbot.paper_core.cycle_diagnostics import TraderCycleDiagnostics

    db, store, broker, *_ = prepare(tmp_path)
    store.clocks = SimpleNamespace(process_id="hook-failure", sample=lambda: (NOW, 100.0))

    async def acquire():
        return cohort()

    def broken(*args, **kwargs):
        raise ValueError("PRIVATE_TOKEN_ABC123")

    broker.observation_cohort = acquire
    monkeypatch.setattr(TraderCycleDiagnostics, hook, broken)
    runtime = SimpleNamespace(
        entry_poll_seconds=15, guardian_poll_seconds=15, outcomes_poll_seconds=15
    )
    try:
        await roles.run_role(store, broker, runtime, "trader", cycles=1)
        documents = store.documents("core_decisions")
        assert documents
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        heartbeat = next(item for item in lines if item.get("role") == "trader")
        assert "PRIVATE_TOKEN_ABC123" not in str(heartbeat["objections"])
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("result_kind", ["failure", "malformed"])
async def test_real_dispatch_observes_failure_without_diagnostic_rejection(
    tmp_path, monkeypatch, capsys, result_kind
):
    import json

    from quantbot.paper_core.guardian import acknowledge_handoffs
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, model, _ = prepare(tmp_path)
    store.clocks = SimpleNamespace(process_id="dispatch-failure", sample=lambda: (NOW, 100.0))
    store.runtime_role = "trader"
    captured = cohort()
    calls = []

    async def acquire():
        return captured

    async def submit(*args, **kwargs):
        calls.append(args[0].intent_id)
        if result_kind == "failure":
            raise ValueError("PRIVATE_TOKEN_ABC123")
        return object()  # Deliberately malformed result exercises diagnostic isolation only.

    try:
        # Disposable fixture replaces independent-process attestation only; the role,
        # durable claims, receipt lookup and dispatch implementation remain real.
        monkeypatch.setattr("quantbot.paper_core.authority.guardian_objections", lambda *a, **k: ())
        decision = await trader_cycle(store, broker, captured, model, proof, now=NOW)
        assert decision.action == "BUY" and store.links()
        store.runtime_role = "guardian"
        acknowledge_handoffs(store, now=NOW)
        store.runtime_role = "trader"
        broker.observation_cohort = acquire
        monkeypatch.setattr(roles, "guardian_objections", lambda *args, **kwargs: ())
        monkeypatch.setattr(
            roles, "coordinator", lambda *args: SimpleNamespace(submit_durable=submit)
        )
        runtime = SimpleNamespace(
            entry_poll_seconds=15, guardian_poll_seconds=15, outcomes_poll_seconds=15
        )
        await roles.run_role(store, broker, runtime, "trader", cycles=1)
        assert calls
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        event = next(item for item in lines if item.get("event") == "PAPER_TRADER_CYCLE")
        heartbeat = next(item for item in lines if item.get("role") == "trader")
        if result_kind == "failure":
            assert "OPERATIONAL_FAILURE" in event["outcomes"]
            assert "PRIVATE_TOKEN_ABC123" not in str(event)
            assert "ValueError" in heartbeat["objections"]  # Existing authority reasons preserved.
        else:
            assert "OPERATIONAL_FAILURE" not in event["outcomes"]
            assert "AttributeError" not in heartbeat["objections"]
            assert "FILL_CONFIRMED" not in event["outcomes"]
    finally:
        db.close()
