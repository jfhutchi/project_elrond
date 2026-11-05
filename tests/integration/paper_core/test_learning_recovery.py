"""Offline V3 recovery: unchanged outcomes can acquire newly eligible work state."""

import asyncio
from datetime import timedelta
from time import perf_counter

import pytest
from sqlalchemy import func, select
from tests.integration.paper_core.test_daily_paper_loop import (
    SESSIONS,
    DailyProvider,
    advance_to,
    catch_up_provider,
    selection,
)
from tests.integration.paper_core.test_paper_funds import close, enter, prepare

from quantbot.paper_core import learning, roles
from quantbot.paper_core.contracts import LearningReceipt
from quantbot.paper_core.learning import _select, consider_learning
from quantbot.paper_core.outcomes import process_outcomes
from quantbot.paper_core.roles import daily_consideration
from quantbot.paper_core.store import table
from quantbot.paper_core.work import dirty
from quantbot.research.registry import HypothesisRegistry


def outcome_snapshot(db):
    with db.transaction() as session:
        index = table("core_outcome_index")
        return tuple(session.execute(
            select(index.c.outcome_id, index.c.status).order_by(index.c.lot_id)
        ))


@pytest.mark.asyncio
async def test_acknowledged_no_change_reconsiders_reconciled_unchanged_gross_outcomes(
    tmp_path, monkeypatch
):
    """A cached insufficient-input acknowledgement must not hide later eligibility."""
    db, store, broker, provider, clocks = await prepare(tmp_path, provider_factory=DailyProvider)
    try:
        decisions = []
        for day_index, session_open in enumerate(SESSIONS[:10]):
            for offset in range(6):
                advance_to(clocks, session_open + timedelta(minutes=35 + offset * 40, seconds=10))
                if offset == 0 and day_index:
                    await catch_up_provider(store, broker, clocks)
                sign = 1 if offset % 2 == 0 else -1
                provider.signs = dict.fromkeys(("AAPL", "MSFT"), sign)
                provider.exit_sign = sign
                decision, reasons = await enter(store, broker)
                assert decision.action == "BUY", decision.gates
                assert not reasons, reasons
                decisions.append(decision)
                await close(store, broker, clocks)
                process_outcomes(store, cutoff=clocks.wall)
        advance_to(clocks, SESSIONS[9] + timedelta(hours=6, minutes=30, seconds=3))
        original = outcome_snapshot(db)
        assert len(original) == 60
        with db.transaction() as session:
            dirty(session, (d.id for d in decisions), available_at=clocks.wall)

        first = await daily_consideration(store, broker, broker.config)
        _, acknowledged = selection(store)
        refused = store.read("core_learning_runs", acknowledged.receipt_id, LearningReceipt)
        assert first[-1] == "INSUFFICIENT_EPISODES_OR_SESSIONS"
        assert refused.status == "NO_CHANGE" and refused.trial_charge_id is None
        assert acknowledged.action == "ACKNOWLEDGE"
        assert len(refused.exclusions) == 60
        assert {reason for _, reason in refused.exclusions} == {"OUTCOME_RECONCILIATION_PENDING"}

        # The production resolver clears dirty work without revising identical economics.
        for _ in range(4):
            process_outcomes(store, cutoff=clocks.wall)
        assert outcome_snapshot(db) == original
        with db.transaction() as session:
            work = table("core_outcome_work")
            assert session.execute(select(func.count()).select_from(work).where(
                work.c.version > work.c.processed_version
            )).scalar_one() == 0
            eligible, excluded, _ = _select(
                session, refused.policy, refused.cutoff, execution_now=clocks.wall
            )
            assert len(eligible) == 60 and not excluded

        fits = []
        original_fit = learning._fit_subprocess

        def counted_fit(*args, **kwargs):
            fits.append(True)
            return original_fit(*args, **kwargs)

        monkeypatch.setattr(learning, "_fit_subprocess", counted_fit)
        recovered = await daily_consideration(store, broker, broker.config)
        _, after = selection(store)
        # The same recovered corpus replays the unmodified fitter's exact receipt.
        direct = consider_learning(
            store, refused.policy, cutoff=refused.cutoff,
            execution_clock=clocks, calendar=await broker.learning_calendar(),
        )
        assert direct.status == "AWAITING_ACTIVATION", direct.reason
        assert direct.id == after.receipt_id
        assert recovered[-1] == "PASSED_FIXED_PROCEDURE_GATES", (
            "daily_consideration reused an acknowledged refusal after unchanged outcomes "
            "became eligible", recovered, direct.status,
        )
        assert after.receipt_id != refused.id
        assert len(fits) == 1

        # Activation changes the parent policy once. Acknowledge its reused inputs,
        # then the authoritative receipt must replay without another fit or charge.
        settled = await daily_consideration(store, broker, broker.config)
        assert settled[-1] == "SESSION_BUDGET_OR_INPUT_REUSED"
        _, settled_activation = selection(store)
        settled_receipt = store.read(
            "core_learning_runs", settled_activation.receipt_id, LearningReceipt
        )
        assert settled_receipt.status == "NO_CHANGE"
        assert settled_receipt.trial_charge_id is None
        with db.transaction() as session:
            receipt_count = session.execute(
                select(func.count()).select_from(table("core_learning_runs"))
            ).scalar_one()
            assert session.execute(
                select(func.count()).select_from(table("core_learning_attempts"))
            ).scalar_one() == 1
            assert HypothesisRegistry(session).learner_trials_spent() == 1

        timings = []
        for _ in range(4):
            started = perf_counter()
            assert await daily_consideration(store, broker, broker.config) == settled
            timings.append(perf_counter() - started)
            assert selection(store)[1].id == settled_activation.id
        print("SETTLED_REPLAY_SECONDS:" + ",".join(f"{value:.6f}" for value in timings))
        assert len(fits) == 1
        assert store.read(
            "core_learning_runs", settled_receipt.id, LearningReceipt
        ) == settled_receipt
        with db.transaction() as session:
            assert session.execute(
                select(func.count()).select_from(table("core_learning_runs"))
            ).scalar_one() == receipt_count
            assert session.execute(
                select(func.count()).select_from(table("core_learning_attempts"))
            ).scalar_one() == 1
            assert HypothesisRegistry(session).learner_trials_spent() == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_outcomes_role_reconsiders_unchanged_inputs_with_one_learning_task(
    tmp_path, monkeypatch
):
    """A settled session/revision snapshot must not suppress learner admission."""
    db, store, broker, _, clocks = await prepare(tmp_path, provider_factory=DailyProvider)
    advance_to(clocks, SESSIONS[0] + timedelta(hours=6, minutes=30, seconds=3))
    original = outcome_snapshot(db)
    release = asyncio.Event()
    original_sleep = asyncio.sleep
    calls = active = maximum_active = polls = 0
    events = []
    original_process = roles.process_outcomes

    def process(*args, **kwargs):
        events.append("outcomes")
        return original_process(*args, **kwargs)

    async def consider(actual_store, actual_broker, actual_config):
        nonlocal calls, active, maximum_active
        assert (actual_store, actual_broker, actual_config) == (store, broker, broker.config)
        assert events[-1] == "outcomes"
        events.append("learning")
        calls += 1
        active += 1
        maximum_active = max(maximum_active, active)
        try:
            await release.wait()
            return ("CONSIDERED_SESSION:2026-10-14", "INSUFFICIENT_EPISODES_OR_SESSIONS")
        finally:
            active -= 1

    async def advance(seconds):
        nonlocal polls
        polls += 1
        clocks.advance(seconds)
        await original_sleep(0)
        if polls == 2:
            assert calls == 1 and active == 1
            release.set()
            await original_sleep(0)

    monkeypatch.setattr(roles, "process_outcomes", process)
    monkeypatch.setattr(roles, "daily_consideration", consider)
    monkeypatch.setattr(roles.asyncio, "sleep", advance)
    try:
        await roles.run_role(store, broker, broker.config, "outcomes", cycles=4)
        assert calls == 3
        assert maximum_active == 1 and active == 0
        assert events.count("outcomes") == 4
        assert outcome_snapshot(db) == original
    finally:
        release.set()
        db.close()
