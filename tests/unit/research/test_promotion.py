"""What a strategy has to earn, and the step this system cannot take at all."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quantbot.domain import (
    Account,
    Bar,
    BrokerOrder,
    Fill,
    IntentState,
    OrderIntent,
    OrderSide,
    OrderType,
    StrategyIdentity,
    TimeInForce,
)
from quantbot.research.builder import OutcomeVerdict
from quantbot.research.manifest import ExecutionPath, StatisticalTest
from quantbot.research.memory import RecordKind, ResearchMemory, ResearchRecord, Verdict
from quantbot.research.power import PowerVerdict
from quantbot.research.promotion import (
    ALLOWED_PROMOTIONS,
    MINIMUM_FORWARD_DAYS,
    MINIMUM_FORWARD_TRADES,
    DeploymentRole,
    ForwardEvidence,
    ForwardObservation,
    ForwardVerdict,
    LedgerObservations,
    PromotionLedger,
    PromotionRefused,
    PromotionState,
    Stage,
    assess_forward_evidence,
    count_forward_days,
    demote,
    demote_on_integrity_incidents,
    forward_progress,
    material_change,
    observe_forward_days,
    promote,
    survivor_objections,
)
from quantbot.research.registry import DataRole, HypothesisRegistry
from quantbot.storage import Database, StorageRepository
from tests.unit.research.test_builder import (  # reuse the compiled plan and outcome fixtures
    outcome,
)
from tests.unit.research.test_registry import (  # a frozen claim, frozen the way the registry does
    make_draft,
    register,
    sharpe_effect,
    window,
)

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=UTC)


def state(stage: Stage = Stage.REGISTERED, **overrides: object) -> PromotionState:
    fields: dict[str, object] = {
        "strategy_id": "adaptive-momentum",
        "stage": stage,
        "strategy_version": "1.3.0",
        "configuration_hash": "cfg-abc",
        "reason": "fixture",
        "actor": "director",
        "updated_at": NOW,
    }
    fields.update(overrides)
    return PromotionState(**fields)


def hand_built(
    days: int, *, trades_each: int = 1, version: str = "1.3.0", config: str = "cfg-abc"
) -> list[ForwardObservation]:
    """Forward observations nobody traded. Every counter must refuse these."""
    return [
        ForwardObservation(
            strategy_id="adaptive-momentum",
            trading_date=date(2026, 8, 19) + timedelta(days=index),
            trades=trades_each,
            role=DataRole.FORWARD_PAPER,
            strategy_version=version,
            configuration_hash=config,
        )
        for index in range(days)
    ]


def seed_ledger(
    session,
    *,
    days: int,
    trades_each: int = 1,
    strategy_id: str = "adaptive-momentum",
    version: str = "1.3.0",
    config: str = "cfg-abc",
) -> None:
    """Write the durable facts a real trading day leaves behind.

    A deployment, one qualified `qualification_days` row per session, and real intent/order/fill
    rows for the trades. This is deliberately laborious: it is the whole point that the only way
    to make the forward counter move is to make the ledger say a strategy traded.
    """
    repository = StorageRepository(session)
    repository.save_strategy_deployment(
        StrategyIdentity(
            strategy_id=strategy_id,
            version=version,
            git_commit="abc1234",
            configuration_hash=config,
            deployment_timestamp=NOW,
        )
    )
    for index in range(days):
        trading_date = date(2026, 8, 19) + timedelta(days=index)
        repository.record_qualification_day(strategy_id, trading_date, qualified=True)
        for trade in range(trades_each):
            suffix = f"{index}-{trade}"
            repository.create_order_intent(
                OrderIntent(
                    intent_id=f"intent-{suffix}",
                    client_order_id=f"client-{suffix}",
                    strategy_id=strategy_id,
                    symbol="SPY",
                    signal_date=trading_date,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    time_in_force=TimeInForce.DAY,
                    quantity="1",
                    created_at=NOW,
                    state=IntentState.RISK_APPROVED,
                )
            )
            repository.save_broker_order(
                BrokerOrder(
                    broker_order_id=f"broker-{suffix}",
                    client_order_id=f"client-{suffix}",
                    symbol="SPY",
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    time_in_force=TimeInForce.DAY,
                    quantity="1",
                    filled_quantity="1",
                    status="filled",
                    submitted_at=NOW,
                )
            )
            repository.record_fill(
                Fill(
                    fill_id=f"fill-{suffix}",
                    broker_order_id=f"broker-{suffix}",
                    symbol="SPY",
                    side=OrderSide.BUY,
                    quantity="1",
                    price="500.00",
                    occurred_at=NOW + timedelta(days=index, minutes=trade),
                    fee="0",
                )
            )


def test_there_is_no_transition_into_live_trading() -> None:
    """Implemented by the absence of a destination, not by a permission check."""
    assert "LIVE" not in {stage.value for stage in Stage}
    assert ALLOWED_PROMOTIONS[Stage.LIVE_REVIEW_ELIGIBLE] == frozenset()

    eligible = state(Stage.LIVE_REVIEW_ELIGIBLE)
    assert eligible.eligible_for_human_review
    for stage in Stage:
        with pytest.raises(PromotionRefused, match="does not lead to"):
            promote(eligible, stage, actor="director", reason="go live", now=NOW)


def test_a_stage_cannot_be_skipped() -> None:
    with pytest.raises(PromotionRefused, match="does not lead to"):
        promote(state(Stage.CANDIDATE), Stage.SHADOW, actor="director", reason="x", now=NOW)
    with pytest.raises(PromotionRefused, match="does not lead to"):
        promote(
            state(Stage.RESEARCH_SURVIVOR),
            Stage.PAPER_QUALIFIED,
            actor="director",
            reason="x",
            now=NOW,
        )


def test_passing_a_pre_registered_test_is_not_enough_to_survive() -> None:
    """Cycle 11's two candidates passed their stated tests and were both wrong.

    The flaw was in the statistic, not the protocol, so robustness and regime diagnostics
    would not have caught either.
    """
    # An independent-samples test on paired data: the overnight-effect failure.
    wrong_statistic = outcome(
        plan=outcome().plan.model_copy(
            update={
                "statistics": outcome().plan.statistics.model_copy(
                    update={"test": StatisticalTest.ONE_SAMPLE_T}
                )
            }
        )
    )
    objections = survivor_objections(wrong_statistic)
    assert any("not valid for PAIRED data" in item for item in objections)
    with pytest.raises(PromotionRefused, match="not valid for PAIRED"):
        promote(
            state(),
            Stage.RESEARCH_SURVIVOR,
            actor="director",
            reason="clears the bar",
            now=NOW,
            outcome=wrong_statistic,
        )

    # A probe that objected: the cross-asset generality failure that killed vol targeting.
    probed = outcome(verdict=OutcomeVerdict.REFUTED, probes_failed=("cross-asset-generality",))
    assert any("cross-asset-generality" in item for item in survivor_objections(probed))

    # And a clean result does survive, so the gate is not simply refusing everything.
    promoted = promote(
        state(),
        Stage.RESEARCH_SURVIVOR,
        actor="director",
        reason="cleared every probe",
        now=NOW,
        outcome=outcome(),
    )
    assert promoted.stage is Stage.RESEARCH_SURVIVOR


def test_a_standalone_study_cannot_make_a_survivor() -> None:
    standalone = outcome(
        plan=outcome().plan.model_copy(update={"execution_path": ExecutionPath.RESEARCH_SCRIPT}),
        verdict=OutcomeVerdict.INCONCLUSIVE,
    )
    assert any("standalone model" in item for item in survivor_objections(standalone))


def test_a_backtest_can_never_increment_the_forward_counter() -> None:
    """The exact failure the project goal document was written to prevent."""
    for role in (DataRole.DISCOVERY, DataRole.VALIDATION, DataRole.PROTECTED_EVALUATION):
        with pytest.raises(ValueError, match="cannot increment this counter"):
            ForwardObservation(
                strategy_id="adaptive-momentum",
                trading_date=date(2026, 8, 19),
                trades=1,
                role=role,
                strategy_version="1.3.0",
                configuration_hash="cfg-abc",
            )


def test_paper_qualification_needs_the_full_authentic_window(ledger_db: Database) -> None:
    """The account holds a handful of forward days against a thirty-day window."""
    one_day = state(Stage.PAPER_OBSERVATION)

    with ledger_db.transaction() as session:
        seed_ledger(session, days=1)
        with pytest.raises(PromotionRefused, match="1 authentic forward days") as error:
            promote(
                one_day,
                Stage.PAPER_QUALIFIED,
                actor="director",
                reason="seventeen research cycles",
                now=NOW,
                observations=observe_forward_days(
                    session,
                    strategy_id="adaptive-momentum",
                    strategy_version="1.3.0",
                    configuration_hash="cfg-abc",
                ),
            )
        assert "30 and 30 required" in str(error.value)

    # And clearing the floor is where the authenticity question ends, not where qualification
    # begins. Thirty days and thirty trades used to reach PAPER_QUALIFIED on their own; now the
    # same call refuses, because nothing has said whether any of it made money (#47).
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        with pytest.raises(PromotionRefused, match="no measured forward evidence"):
            promote(
                one_day,
                Stage.PAPER_QUALIFIED,
                actor="director",
                reason="window complete",
                now=NOW,
                observations=observe_forward_days(
                    session,
                    strategy_id="adaptive-momentum",
                    strategy_version="1.3.0",
                    configuration_hash="cfg-abc",
                ),
            )


def test_thirty_sessions_without_trading_is_not_qualification(ledger_db: Database) -> None:
    """Days and trades are separate counters, and the ledger supplies both separately.

    A strategy that was live for thirty sessions and submitted nothing has thirty days of
    evidence that it does not trade, which is not thirty days of evidence that it works.
    """
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS, trades_each=0)
        with pytest.raises(PromotionRefused, match="30 authentic forward days and 0 trades"):
            promote(
                state(Stage.PAPER_OBSERVATION),
                Stage.PAPER_QUALIFIED,
                actor="director",
                reason="days but no trading",
                now=NOW,
                observations=observe_forward_days(
                    session,
                    strategy_id="adaptive-momentum",
                    strategy_version="1.3.0",
                    configuration_hash="cfg-abc",
                ),
            )


def test_forward_observations_nobody_traded_cannot_reach_the_counter() -> None:
    """The #16 gap: `ForwardObservation` refused a backtest role but not a fabricated day.

    Forward evidence is the only category in this project that cannot be regenerated. A bad
    backtest can be re-run; a trading day that did not happen cannot be un-invented. So the
    counter takes `LedgerObservations`, which only `observe_forward_days` can produce.

    The runtime check is the mechanism. The annotation alone would let a perfectly well-formed
    list of thirty `ForwardObservation`s through, which is exactly the attack.
    """
    fabricated = hand_built(MINIMUM_FORWARD_DAYS)

    with pytest.raises(TypeError, match="not forward evidence"):
        count_forward_days(
            fabricated,  # type: ignore[arg-type]
            strategy_version="1.3.0",
            configuration_hash="cfg-abc",
        )

    with pytest.raises(TypeError, match="not forward evidence"):
        promote(
            state(Stage.PAPER_OBSERVATION),
            Stage.PAPER_QUALIFIED,
            actor="director",
            reason="thirty days, allegedly",
            now=NOW,
            observations=fabricated,  # type: ignore[arg-type]
        )

    # Nor by constructing the wrapper directly around the same fabricated list.
    with pytest.raises(TypeError, match="cannot be constructed, only observed"):
        LedgerObservations(fabricated, token=object())


def test_a_deployment_the_ledger_never_recorded_yields_no_forward_evidence(
    ledger_db: Database,
) -> None:
    """Qualified days alone are not enough: they must belong to *this* identity.

    Without the deployment check a strategy could inherit the trading history of whatever else
    ran under the same id, which is the material-change rule failing open at the source.
    """
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS, version="1.3.0", config="cfg-abc")

        same = observe_forward_days(
            session,
            strategy_id="adaptive-momentum",
            strategy_version="1.3.0",
            configuration_hash="cfg-abc",
        )
        assert len(same) == MINIMUM_FORWARD_DAYS

        # Same version, different configuration: a different strategy wearing the same number.
        reconfigured = observe_forward_days(
            session,
            strategy_id="adaptive-momentum",
            strategy_version="1.3.0",
            configuration_hash="cfg-changed",
        )
        assert len(reconfigured) == 0

        # A version that never deployed at all.
        unknown = observe_forward_days(
            session,
            strategy_id="adaptive-momentum",
            strategy_version="9.9.9",
            configuration_hash="cfg-abc",
        )
        assert len(unknown) == 0


def test_the_durable_ladder_reads_forward_evidence_and_refuses_to_be_told_it(
    ledger_db: Database,
) -> None:
    """`PromotionLedger` has a session, so it goes and looks rather than asking the caller."""
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS, strategy_id="adaptive-momentum")
        ladder = PromotionLedger(session)
        ladder.enter(state(Stage.PAPER_OBSERVATION))

        for supplied in ("observations", "evidence"):
            with pytest.raises(
                PromotionRefused, match="read from the durable ledger, not supplied"
            ):
                ladder.promote(
                    "adaptive-momentum",
                    Stage.PAPER_QUALIFIED,
                    actor="director",
                    reason="I brought my own",
                    now=NOW,
                    account_id=ACCOUNT,
                    benchmark_symbol=BENCHMARK,
                    **{supplied: hand_built(MINIMUM_FORWARD_DAYS)},
                )

        # Where to look is a caller's to say; what was found is not. Without an account and a
        # benchmark there is nothing to read the economics out of, and absolute P&L cannot tell
        # +4% against +8% from an edge.
        with pytest.raises(PromotionRefused, match="needs an account to read equity from"):
            ladder.promote(
                "adaptive-momentum",
                Stage.PAPER_QUALIFIED,
                actor="director",
                reason="just the counts, please",
                now=NOW,
            )

        # The ledger looks, and refuses on what it finds: no prices were ever written, so there
        # is no statistic and no frozen claim behind this deployment.
        with pytest.raises(PromotionRefused, match="no frozen economic claim"):
            ladder.promote(
                "adaptive-momentum",
                Stage.PAPER_QUALIFIED,
                actor="director",
                reason="the counts agree",
                now=NOW,
                account_id=ACCOUNT,
                benchmark_symbol=BENCHMARK,
            )


def test_a_material_change_restarts_the_window_by_excluding_the_old_days(
    ledger_db: Database,
) -> None:
    """The observations were of a different strategy, so they are not observations of this one."""
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        collected = observe_forward_days(
            session,
            strategy_id="adaptive-momentum",
            strategy_version="1.3.0",
            configuration_hash="cfg-abc",
        )
    days, trades = count_forward_days(
        collected, strategy_version="1.3.0", configuration_hash="cfg-abc"
    )
    assert (days, trades) == (30, 30)

    after, _ = count_forward_days(collected, strategy_version="1.4.0", configuration_hash="cfg-abc")
    assert after == 0

    changed = material_change(
        state(Stage.PAPER_QUALIFIED),
        strategy_version="1.4.0",
        configuration_hash="cfg-def",
        now=NOW,
    )
    assert changed.stage is Stage.RESEARCH_SURVIVOR
    assert "restarted" in changed.reason
    # An unchanged identity leaves the state exactly as it was.
    assert (
        material_change(
            state(Stage.PAPER_QUALIFIED),
            strategy_version="1.3.0",
            configuration_hash="cfg-abc",
            now=NOW,
        ).stage
        is Stage.PAPER_QUALIFIED
    )


def test_an_unresolved_integrity_violation_blocks_every_promotion() -> None:
    with pytest.raises(PromotionRefused, match="evidence-integrity violation"):
        promote(
            state(Stage.RESEARCH_SURVIVOR),
            Stage.SHADOW,
            actor="director",
            reason="looks fine",
            now=NOW,
            integrity_clear=False,
        )


def test_demotion_is_not_gated() -> None:
    """A system slow to stop trusting something is worse than one occasionally too quick."""
    dropped = demote(
        state(Stage.PAPER_QUALIFIED),
        Stage.CANDIDATE,
        actor="supervisor",
        reason="reconciliation defect found in the observation window",
        now=NOW,
    )
    assert dropped.stage is Stage.CANDIDATE

    with pytest.raises(PromotionRefused, match="is not below"):
        demote(
            state(Stage.SHADOW),
            Stage.LIVE_REVIEW_ELIGIBLE,
            actor="agent",
            reason="promote via demote",
            now=NOW,
        )


def test_forward_progress_reports_the_honest_distance(ledger_db: Database) -> None:
    with ledger_db.transaction() as session:
        seed_ledger(session, days=1)
        observed = observe_forward_days(
            session,
            strategy_id="adaptive-momentum",
            strategy_version="1.3.0",
            configuration_hash="cfg-abc",
        )
    progress = forward_progress(observed, strategy_version="1.3.0", configuration_hash="cfg-abc")
    assert progress["forward_days"] == Decimal("1")
    assert progress["days_required"] == Decimal("30")
    assert progress["forward_days"] < progress["days_required"]


@pytest.fixture
def ledger_db(tmp_path):
    db = Database(tmp_path / "ladder.db")
    yield db
    db.close()


def _entry(stage: Stage = Stage.CANDIDATE) -> PromotionState:
    return PromotionState(
        strategy_id="adaptive-momentum-v1-309894d8d8a5296e",
        stage=stage,
        strategy_version="1.2.0",
        configuration_hash="cfg-hash",
        reason="entered the ladder",
        actor="hutch",
        updated_at=NOW,
    )


def test_a_ladder_position_outlives_the_process_that_computed_it(ledger_db) -> None:
    """#16: `promote()` returned a state and nothing could store it.

    A restart previously reset every strategy to whatever a caller happened to construct next,
    which for a record of what a strategy earned is the same as having no record.
    """
    with ledger_db.transaction() as session:
        PromotionLedger(session).enter(_entry())

    with ledger_db.transaction() as session:
        restored = PromotionLedger(session).current("adaptive-momentum-v1-309894d8d8a5296e")

    assert restored is not None
    assert restored.stage is Stage.CANDIDATE
    assert restored.strategy_version == "1.2.0"


def test_the_ledger_enforces_the_same_transitions_as_the_pure_function(ledger_db) -> None:
    """The rules are not duplicated in the ledger; it delegates to `promote`.

    A second copy of the transition table would eventually disagree with the first, and the one
    in `ALLOWED_PROMOTIONS` is the one that has tests.
    """
    with ledger_db.transaction() as session:
        ledger = PromotionLedger(session)
        ledger.enter(_entry())
        with pytest.raises(PromotionRefused, match="does not lead to"):
            ledger.promote(
                "adaptive-momentum-v1-309894d8d8a5296e",
                Stage.PAPER_QUALIFIED,
                actor="hutch",
                reason="skipping the queue",
                now=NOW,
            )


def test_a_refused_promotion_writes_no_history(ledger_db) -> None:
    """A promotion that did not happen is not a move.

    Recording the attempt would put refused transitions in the same history as earned ones, and
    the history is what the ladder's claim rests on.
    """
    with ledger_db.transaction() as session:
        ledger = PromotionLedger(session)
        ledger.enter(_entry())
        with pytest.raises(PromotionRefused):
            ledger.promote(
                "adaptive-momentum-v1-309894d8d8a5296e",
                Stage.SHADOW,
                actor="hutch",
                reason="not earned",
                now=NOW,
            )

    with ledger_db.transaction() as session:
        events = PromotionLedger(session).history("adaptive-momentum-v1-309894d8d8a5296e")

    assert [event.to_stage for event in events] == ["CANDIDATE"]


def test_re_entering_the_ladder_is_refused_rather_than_silently_resetting(ledger_db) -> None:
    """Re-entry would discard everything the strategy earned, through the call that starts one."""
    with ledger_db.transaction() as session:
        ledger = PromotionLedger(session)
        ledger.enter(_entry())
        ledger.promote(
            "adaptive-momentum-v1-309894d8d8a5296e",
            Stage.REGISTERED,
            actor="hutch",
            reason="registered",
            now=NOW,
        )
        with pytest.raises(PromotionRefused, match="already on the ladder"):
            ledger.enter(_entry())

    with ledger_db.transaction() as session:
        assert (
            PromotionLedger(session).current("adaptive-momentum-v1-309894d8d8a5296e").stage
            is Stage.REGISTERED
        )


def test_a_strategy_that_never_entered_cannot_move(ledger_db) -> None:
    """Promoting an unknown strategy would create a position it never earned."""
    with ledger_db.transaction() as session:
        with pytest.raises(PromotionRefused, match="not on the ladder"):
            PromotionLedger(session).promote(
                "never-seen", Stage.REGISTERED, actor="hutch", reason="x", now=NOW
            )


def _raise_incident(
    session,
    *,
    incident_id: str,
    severity: str = "ERROR",
    strategy_id: str | None = "adaptive-momentum",
    resolved: bool = False,
) -> None:
    """One incident, optionally attached to a run so it can be attributed to a strategy."""
    repository = StorageRepository(session)
    run_id: str | None = None
    if strategy_id is not None:
        run_id = f"run-for-{incident_id}"
        repository.create_run(
            run_id,
            StrategyIdentity(
                strategy_id=strategy_id,
                version="1.3.0",
                git_commit="abc1234",
                configuration_hash="cfg-abc",
                deployment_timestamp=NOW,
            ),
            started_at=NOW,
        )
    repository.create_incident(
        incident_id,
        severity=severity,
        kind="RECONCILIATION_FAILED",
        message="broker and ledger disagree",
        occurred_at=NOW,
        run_id=run_id,
        resolved_at=NOW if resolved else None,
    )


def test_an_unresolved_integrity_incident_demotes_out_of_the_forward_track(
    ledger_db: Database,
) -> None:
    """#16: `demote()` was ungated and nothing called it when an incident landed.

    So the ladder could report a strategy as PAPER_QUALIFIED while its own reconciliation was
    failing, which is the exact failure the ladder exists to prevent. A rule nobody invokes is
    not a control.
    """
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        ladder = PromotionLedger(session)
        ladder.enter(state(Stage.PAPER_QUALIFIED))
        _raise_incident(session, incident_id="INC-1")

        sweep = demote_on_integrity_incidents(session, now=NOW)

        assert not sweep.clean
        assert [item.strategy_id for item in sweep.demoted] == ["adaptive-momentum"]
        assert ladder.current("adaptive-momentum").stage is Stage.RESEARCH_SURVIVOR


def test_demotion_does_not_erase_the_forward_evidence_that_really_happened(
    ledger_db: Database,
) -> None:
    """An incident casts doubt on evidence; it does not make the sessions un-occur.

    Deleting the days would be its own kind of fabrication, and it would make the demotion
    irreversible for a problem that turns out to be a bad broker feed.
    """
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        PromotionLedger(session).enter(state(Stage.PAPER_QUALIFIED))
        _raise_incident(session, incident_id="INC-1")
        demote_on_integrity_incidents(session, now=NOW)

        still_there = observe_forward_days(
            session,
            strategy_id="adaptive-momentum",
            strategy_version="1.3.0",
            configuration_hash="cfg-abc",
        )

    days, trades = count_forward_days(
        still_there, strategy_version="1.3.0", configuration_hash="cfg-abc"
    )
    assert (days, trades) == (MINIMUM_FORWARD_DAYS, MINIMUM_FORWARD_DAYS)


def test_an_informational_or_resolved_incident_demotes_nothing(ledger_db: Database) -> None:
    """Otherwise every routine INFO note would knock a strategy off the ladder.

    A control that fires on everything is one an operator turns off, which leaves the real
    incidents unhandled -- so the severity filter is part of the protection, not a softening
    of it.
    """
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        ladder = PromotionLedger(session)
        ladder.enter(state(Stage.PAPER_QUALIFIED))
        _raise_incident(session, incident_id="INC-INFO", severity="INFO")
        _raise_incident(session, incident_id="INC-FIXED", severity="ERROR", resolved=True)

        sweep = demote_on_integrity_incidents(session, now=NOW)

        assert sweep.clean
        assert ladder.current("adaptive-momentum").stage is Stage.PAPER_QUALIFIED


def test_an_incident_nobody_can_attribute_is_reported_rather_than_dropped(
    ledger_db: Database,
) -> None:
    """A sweep that demoted nothing must not read as a clean system.

    An incident raised outside a run has no strategy to demote. Returning only what was demoted
    would let an operator read silence as safety, so the count travels in the same result as
    the action and `clean` accounts for both.
    """
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        ladder = PromotionLedger(session)
        ladder.enter(state(Stage.PAPER_QUALIFIED))
        _raise_incident(session, incident_id="INC-ORPHAN", strategy_id=None)

        sweep = demote_on_integrity_incidents(session, now=NOW)

        assert sweep.demoted == ()
        assert sweep.unattributed == ("INC-ORPHAN",)
        assert not sweep.clean, "nothing demoted is not the same as nothing wrong"
        # It belongs to no strategy, so no strategy is moved for it.
        assert ladder.current("adaptive-momentum").stage is Stage.PAPER_QUALIFIED


def test_a_repeated_sweep_records_no_second_move(ledger_db: Database) -> None:
    """History holds moves. A strategy already at the floor did not move again."""
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        ladder = PromotionLedger(session)
        ladder.enter(state(Stage.PAPER_QUALIFIED))
        _raise_incident(session, incident_id="INC-1")

        first = demote_on_integrity_incidents(session, now=NOW)
        second = demote_on_integrity_incidents(session, now=NOW)

        assert len(first.demoted) == 1
        assert second.demoted == ()
        demotions = [
            event
            for event in ladder.history("adaptive-momentum")
            if event.direction == "DEMOTION"
        ]
    assert len(demotions) == 1, "one incident, one recorded demotion"


# ---------------------------------------------------------------------------------------------
# Forward economic evidence (#47). Thirty authentic days are an authenticity fact; the tests
# below are about the separate question of whether those days say anything.
# ---------------------------------------------------------------------------------------------

#: 68 cumulative trials put the luck bar at 2.905, and a SHARPE claim needs
#: `(z*sqrt(252)/SR)^2` sessions, so ~29 paired sessions can only resolve an annualised Sharpe
#: of about 8.5. No strategy has one. That is not a fixture convenience -- it is the finding:
#: **a thirty-session window cannot support any edge claim worth making**, and the only way to
#: exercise the supported path at all is to declare an effect nobody could have.
RESOLVABLE_AT_THIRTY_SESSIONS = Decimal("12.0")

BENCHMARK = "SPY"
ACCOUNT = "paper-account-1"


def seed_prices(
    session,
    *,
    days: int,
    strategy_daily_bps: int,
    benchmark_daily_bps: int,
    account_id: str = ACCOUNT,
    jitter_bps: int = 3,
) -> None:
    """An account equity path and a benchmark close path over the same sessions.

    Both series get a little alternating jitter so the excess has a dispersion to divide by. A
    constant excess has no sampling distribution, and `_forward_statistics` refuses it rather
    than reporting an infinite t -- which would be the most flattering arithmetic available here.

    The equity mark is stamped at 20:05 New York, i.e. after the close, which is the shape the
    real cycle writes. Its UTC date is therefore the *following* day, and a gate keying on that
    would credit a session that had not happened.
    """
    repository = StorageRepository(session)
    equity = Decimal("100")
    close = Decimal("500")
    for index in range(days):
        trading_date = date(2026, 8, 19) + timedelta(days=index)
        jitter = jitter_bps if index % 2 == 0 else -jitter_bps
        equity *= 1 + Decimal(strategy_daily_bps + jitter) / 10000
        close *= 1 + Decimal(benchmark_daily_bps - jitter) / 10000
        equity = equity.quantize(Decimal("0.0001"))
        close = close.quantize(Decimal("0.0001"))
        repository.save_account_snapshot(
            f"snap-{index}",
            Account(
                account_id=account_id,
                cash=Decimal("1"),
                buying_power=Decimal("1"),
                equity=equity,
                currency="USD",
            ),
            (),
            captured_at=datetime.combine(
                trading_date, time(20, 5), tzinfo=ZoneInfo("America/New_York")
            ).astimezone(UTC),
        )
        repository.save_bars(
            [
                Bar(
                    symbol=BENCHMARK,
                    timestamp=datetime.combine(
                        trading_date, time(16, 0), tzinfo=ZoneInfo("America/New_York")
                    ).astimezone(UTC),
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=Decimal("1000"),
                    adjustment=Decimal("1"),
                )
            ],
            provider="test",
        )


def freeze_claim(
    session,
    *,
    expected: Decimal = RESOLVABLE_AT_THIRTY_SESSIONS,
    hypothesis_id: str = "H-2026-047",
) -> str:
    """Freeze a real registration, because the gate reads the claim rather than accepting one."""
    registry = HypothesisRegistry(session)
    register(
        registry,
        make_draft(
            hypothesis_id=hypothesis_id,
            effect=sharpe_effect(expected=expected),
            windows=(
                window(DataRole.FORWARD_PAPER, "2026-08-19", "2026-12-31", dataset="alpaca-paper"),
            ),
        ),
    )
    return hypothesis_id


def forward_evidence(session, **overrides: object) -> ForwardEvidence:
    kwargs: dict[str, object] = {
        "strategy_id": "adaptive-momentum",
        "strategy_version": "1.3.0",
        "configuration_hash": "cfg-abc",
        "account_id": ACCOUNT,
        "benchmark_symbol": BENCHMARK,
        "assessed_at": NOW,
    }
    kwargs.update(overrides)
    return assess_forward_evidence(session, **kwargs)  # type: ignore[arg-type]


def test_thirty_days_of_losing_trades_satisfy_the_count_and_qualify_for_nothing(
    ledger_db: Database,
) -> None:
    """The defect #47 names, in one test.

    Every authenticity fact is present: a deployment, thirty qualified sessions, thirty fills
    reached through `order_intents`. The strategy lost money on all of them. Before this gate
    existed that combination reached `PAPER_QUALIFIED`, a label an operator reads as "the paper
    evidence supports this".
    """
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS, trades_each=1)
        seed_prices(
            session,
            days=MINIMUM_FORWARD_DAYS,
            strategy_daily_bps=-20,
            benchmark_daily_bps=5,
        )
        claim = freeze_claim(session)
        evidence = forward_evidence(session, hypothesis_id=claim)

        assert evidence.forward_days == MINIMUM_FORWARD_DAYS
        assert evidence.forward_trades == MINIMUM_FORWARD_TRADES
        assert evidence.verdict is ForwardVerdict.FORWARD_NEGATIVE
        assert not evidence.supported

        with pytest.raises(PromotionRefused, match="FORWARD_NEGATIVE"):
            promote(
                state(Stage.PAPER_OBSERVATION),
                Stage.PAPER_QUALIFIED,
                actor="director",
                reason="thirty days and thirty trades",
                now=NOW,
                observations=observe_forward_days(
                    session,
                    strategy_id="adaptive-momentum",
                    strategy_version="1.3.0",
                    configuration_hash="cfg-abc",
                ),
                evidence=evidence,
            )


def test_profit_that_loses_to_its_own_benchmark_is_not_an_edge(ledger_db: Database) -> None:
    """+4% against the benchmark's +8% is positive P&L and negative value.

    Absolute forward profit cannot tell those apart, which is why the estimand is the excess
    return and not the return.
    """
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        seed_prices(
            session,
            days=MINIMUM_FORWARD_DAYS,
            strategy_daily_bps=13,
            benchmark_daily_bps=26,
        )
        evidence = forward_evidence(session, hypothesis_id=freeze_claim(session))

    assert evidence.statistics is not None
    assert evidence.statistics.excess_sharpe < 0
    assert evidence.verdict is ForwardVerdict.FORWARD_NEGATIVE


def test_an_underpowered_forward_window_is_not_a_refutation(ledger_db: Database) -> None:
    """`UNDERPOWERED` is not `REFUTED` here either, and the order of the checks is why.

    A claim of an annualised Sharpe of 1.0 needs about 2,100 sessions at this luck bar. Thirty
    cannot resolve it in either direction, so the verdict is decided *before* the point estimate
    is consulted -- reading power off the result is the post-hoc power fallacy, and recording an
    unresolvable window as a refutation teaches a later agent that a mechanism was tested when
    it never was.
    """
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        seed_prices(
            session, days=MINIMUM_FORWARD_DAYS, strategy_daily_bps=-40, benchmark_daily_bps=10
        )
        claim = freeze_claim(session, expected=Decimal("1.0"), hypothesis_id="H-2026-048")
        evidence = forward_evidence(session, hypothesis_id=claim)

    assert evidence.verdict is ForwardVerdict.FORWARD_UNDERPOWERED
    assert evidence.power is not None
    assert evidence.power.verdict is PowerVerdict.UNDERPOWERED
    # Losing badly, and still not a refutation: the window could not have said either way.
    assert evidence.statistics is not None
    assert evidence.statistics.excess_sharpe < 0
    assert evidence.verdict is not ForwardVerdict.FORWARD_NEGATIVE


def test_a_deployment_with_no_frozen_claim_can_never_be_supported(ledger_db: Database) -> None:
    """The currently deployed rotation's exact situation (#53, #54).

    Its mechanism is refuted as `REFUTED.md` #22 -- the momentum ranking carries no measurable
    information -- and it keeps trading, legitimately, as an operational baseline. Authentic
    paper days cannot rehabilitate a mechanism by existing, and with no frozen estimand there is
    no success criterion except one chosen after seeing the P&L.
    """
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        seed_prices(
            session, days=MINIMUM_FORWARD_DAYS, strategy_daily_bps=400, benchmark_daily_bps=1
        )
        evidence = forward_evidence(session)

    assert evidence.verdict is ForwardVerdict.FORWARD_INCONCLUSIVE
    assert any("no frozen economic claim" in reason for reason in evidence.blocking)
    assert not evidence.supported


def test_the_trade_count_cannot_inflate_the_independent_sample(ledger_db: Database) -> None:
    """Thirty fills on three sessions are not thirty observations.

    The unit of evidence is the session, so multiplying fills changes the authenticity counter
    and leaves the statistical one alone. This gate is the one most exposed to the confusion,
    because the trade count sits right there in the qualification rule.
    """
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS, trades_each=1)
        seed_prices(
            session, days=MINIMUM_FORWARD_DAYS, strategy_daily_bps=30, benchmark_daily_bps=5
        )
        claim = freeze_claim(session)
        thin = forward_evidence(session, hypothesis_id=claim)

    # The same sessions, ten times the fills. Nothing new happened in the market.
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS, trades_each=10)
        fat = forward_evidence(session, hypothesis_id=claim)

    assert fat.forward_trades == thin.forward_trades * 10
    assert thin.statistics is not None and fat.statistics is not None
    assert fat.statistics.paired_sessions == thin.statistics.paired_sessions
    assert fat.statistics.excess_t == thin.statistics.excess_t


def test_an_unresolved_integrity_incident_blocks_qualification(ledger_db: Database) -> None:
    """An incident casts doubt on the evidence, so the evidence does not qualify while it stands."""
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        seed_prices(
            session, days=MINIMUM_FORWARD_DAYS, strategy_daily_bps=300, benchmark_daily_bps=1
        )
        _raise_incident(session, incident_id="incident-1", severity="CRITICAL")
        evidence = forward_evidence(
            session, hypothesis_id=freeze_claim(session), max_drawdown_limit_bps=5000
        )

    assert not evidence.supported
    assert any("unresolved integrity incident" in reason for reason in evidence.blocking)


def test_a_check_that_did_not_run_blocks_rather_than_passing(ledger_db: Database) -> None:
    """`unassessed` is a refusal, not a footnote.

    With no frozen drawdown limit the forward drawdown was not compared to anything, and this
    project has been burned repeatedly by a check that quietly did nothing looking exactly like
    a check that passed.
    """
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        seed_prices(
            session, days=MINIMUM_FORWARD_DAYS, strategy_daily_bps=300, benchmark_daily_bps=1
        )
        without_limit = forward_evidence(session, hypothesis_id=freeze_claim(session))

    assert without_limit.unassessed
    assert not without_limit.supported
    assert "not assessed" in without_limit.explain()


def test_an_excess_that_is_only_beta_does_not_qualify(ledger_db: Database) -> None:
    """Cycle 15's finding as a gate.

    A levered benchmark tracker beats the benchmark by construction and adds nothing. Its excess
    return is real and its beta-adjusted alpha is not, so both have to clear the bar rather than
    either one.
    """
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        repository = StorageRepository(session)
        equity = Decimal("100")
        close = Decimal("500")
        for index in range(MINIMUM_FORWARD_DAYS):
            trading_date = date(2026, 8, 19) + timedelta(days=index)
            move = Decimal(60 if index % 3 else -50) / 10000
            close *= 1 + move
            equity *= 1 + move * 2  # exactly 2x the benchmark: all beta, no alpha
            repository.save_account_snapshot(
                f"snap-{index}",
                Account(
                    account_id=ACCOUNT,
                    cash=Decimal("1"),
                    buying_power=Decimal("1"),
                    equity=equity.quantize(Decimal("0.0001")),
                    currency="USD",
                ),
                (),
                captured_at=datetime.combine(
                    trading_date, time(20, 5), tzinfo=ZoneInfo("America/New_York")
                ).astimezone(UTC),
            )
            quantized = close.quantize(Decimal("0.0001"))
            repository.save_bars(
                [
                    Bar(
                        symbol=BENCHMARK,
                        timestamp=datetime.combine(
                            trading_date, time(16, 0), tzinfo=ZoneInfo("America/New_York")
                        ).astimezone(UTC),
                        open=quantized,
                        high=quantized,
                        low=quantized,
                        close=quantized,
                        volume=Decimal("1000"),
                        adjustment=Decimal("1"),
                    )
                ],
                provider="test",
            )
        evidence = forward_evidence(
            session, hypothesis_id=freeze_claim(session), max_drawdown_limit_bps=9000
        )

    assert evidence.statistics is not None
    assert evidence.statistics.beta > Decimal("1.5")
    assert abs(evidence.statistics.alpha_t) < evidence.luck_threshold_z
    assert not evidence.supported
    assert any("index exposure is not an edge" in reason for reason in evidence.blocking)


def test_forward_evidence_cannot_be_assembled_by_a_caller(ledger_db: Database) -> None:
    """The measurement is the type, the same way it is for `LedgerObservations`."""
    with pytest.raises(TypeError, match="cannot be constructed, only measured"):
        ForwardEvidence(
            strategy_id="adaptive-momentum",
            strategy_version="1.3.0",
            configuration_hash="cfg-abc",
            assessed_at=NOW,
            role=DeploymentRole.EDGE_CANDIDATE,
            forward_days=MINIMUM_FORWARD_DAYS,
            forward_trades=MINIMUM_FORWARD_TRADES,
            luck_threshold_z=Decimal("2.905"),
            statistics=None,
            power=None,
            blocking=(),
            unassessed=(),
            verdict=ForwardVerdict.FORWARD_EDGE_SUPPORTED,
            token=object(),
        )

    class Convincing:
        supported = True
        verdict = ForwardVerdict.FORWARD_EDGE_SUPPORTED
        strategy_version = "1.3.0"
        configuration_hash = "cfg-abc"

        def explain(self) -> str:
            return "everything is fine"

    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        observations = observe_forward_days(
            session,
            strategy_id="adaptive-momentum",
            strategy_version="1.3.0",
            configuration_hash="cfg-abc",
        )
    with pytest.raises(TypeError, match="not a measurement"):
        promote(
            state(Stage.PAPER_OBSERVATION),
            Stage.PAPER_QUALIFIED,
            actor="director",
            reason="my object says so",
            now=NOW,
            observations=observations,
            evidence=Convincing(),  # type: ignore[arg-type]
        )


def test_evidence_measured_for_a_different_configuration_does_not_transfer(
    ledger_db: Database,
) -> None:
    """A material change restarts the window, and stale evidence must not survive it."""
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        seed_prices(
            session, days=MINIMUM_FORWARD_DAYS, strategy_daily_bps=300, benchmark_daily_bps=1
        )
        evidence = forward_evidence(
            session, hypothesis_id=freeze_claim(session), configuration_hash="cfg-superseded"
        )
        observations = observe_forward_days(
            session,
            strategy_id="adaptive-momentum",
            strategy_version="1.3.0",
            configuration_hash="cfg-abc",
        )

    with pytest.raises(PromotionRefused, match="the evidence measures"):
        promote(
            state(Stage.PAPER_OBSERVATION),
            Stage.PAPER_QUALIFIED,
            actor="director",
            reason="reusing the old measurement",
            now=NOW,
            observations=observations,
            evidence=evidence,
        )


def test_the_supported_path_exists_and_needs_an_effect_nobody_has(ledger_db: Database) -> None:
    """A gate that always refuses teaches nothing, so the reachable case is pinned too.

    It takes an annualised excess Sharpe past 12 to clear both the power requirement and the
    2.905 luck bar on thirty sessions. Read this test as the arithmetic of the window rather
    than as a plausible strategy: it is the honest reason `PAPER_QUALIFIED` will stay out of
    reach for a long time, and the reason the thirty-day rule was never an economic test.
    """
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        seed_prices(
            session,
            days=MINIMUM_FORWARD_DAYS,
            strategy_daily_bps=120,
            benchmark_daily_bps=1,
            jitter_bps=1,
        )
        evidence = forward_evidence(
            session, hypothesis_id=freeze_claim(session), max_drawdown_limit_bps=2000
        )

        assert evidence.power is not None
        assert evidence.power.cleared, evidence.explain()
        assert evidence.verdict is ForwardVerdict.FORWARD_EDGE_SUPPORTED, evidence.explain()
        assert evidence.supported

        ladder = PromotionLedger(session)
        ladder.enter(state(Stage.PAPER_OBSERVATION))
        moved = ladder.promote(
            "adaptive-momentum",
            Stage.PAPER_QUALIFIED,
            actor="director",
            reason="the forward window agrees",
            now=NOW,
            account_id=ACCOUNT,
            benchmark_symbol=BENCHMARK,
            hypothesis_id="H-2026-047",
            max_drawdown_limit_bps=2000,
        )

    assert moved.stage is Stage.PAPER_QUALIFIED


def test_the_equity_mark_is_attributed_to_the_session_it_closed(ledger_db: Database) -> None:
    """A mark written at 20:05 New York is stored as the next UTC day.

    `docs/research-architecture.md` records this off-by-one for fill attribution, which is why
    trades are credited by `signal_date`. Equity has the same hazard and no `signal_date` to
    lean on: keyed by UTC date, every mark lands one session late, the first and last sessions
    fall off the ends, and the returns are silently shifted by one day against the benchmark.
    """
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        seed_prices(
            session, days=MINIMUM_FORWARD_DAYS, strategy_daily_bps=30, benchmark_daily_bps=5
        )
        evidence = forward_evidence(session, hypothesis_id=freeze_claim(session))

    assert evidence.statistics is not None
    # Thirty qualified sessions, twenty-nine consecutive pairs, none dropped.
    assert evidence.statistics.paired_sessions == MINIMUM_FORWARD_DAYS - 1


def _refute(session, hypothesis_id: str, *, verdict: Verdict = Verdict.REFUTED) -> None:
    """File a refutation against a hypothesis the way a completed research cycle would."""
    ResearchMemory(session).record(
        ResearchRecord(
            record_id=f"refuted-{hypothesis_id}",
            kind=RecordKind.FINDING,
            subject="momentum ranking",
            statement=(
                "The momentum ranking carries no measurable information: alpha against SPY is "
                "0.10%/yr at t=0.05 and top-minus-bottom is t=0.77 against a 2.87 bar."
            ),
            verdict=verdict,
            hypothesis_id=hypothesis_id,
            hypothesis_version=1,
            source="REFUTED.md #22",
            recorded_at=NOW,
        )
    )


def test_a_refuted_mechanism_stops_being_an_edge_candidate(ledger_db: Database) -> None:
    """#53's core path, and the deployed rotation's actual future.

    Real paper days keep arriving after a mechanism is refuted, because the daemon runs beside
    the research system rather than under it. Those days are worth having -- they are where
    slippage, reconciliation and recovery evidence comes from -- and they must not become credit
    toward a label about edge. Authentic execution cannot rehabilitate a mechanism by existing.
    """
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        seed_prices(
            session,
            days=MINIMUM_FORWARD_DAYS,
            strategy_daily_bps=120,
            benchmark_daily_bps=1,
            jitter_bps=1,
        )
        claim = freeze_claim(session)
        before = forward_evidence(
            session, hypothesis_id=claim, max_drawdown_limit_bps=2000
        )
        assert before.role is DeploymentRole.EDGE_CANDIDATE
        assert before.supported, before.explain()

        _refute(session, claim)
        after = forward_evidence(session, hypothesis_id=claim, max_drawdown_limit_bps=2000)

    # Same account, same fills, same profit. The claim is what changed.
    assert after.statistics == before.statistics
    assert after.role is DeploymentRole.OPERATIONAL_BASELINE
    assert not after.supported
    assert any("is refuted by" in reason for reason in after.blocking)


def test_an_untestable_verdict_is_not_a_refutation(ledger_db: Database) -> None:
    """`UNDERPOWERED` does not block, and that is not an oversight.

    It says the data could not resolve the mechanism. Blocking on it would read absence of
    evidence as evidence of absence at the gate -- the collapse research memory refuses to let
    happen in the record, arriving through the back door instead.
    """
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        seed_prices(
            session,
            days=MINIMUM_FORWARD_DAYS,
            strategy_daily_bps=120,
            benchmark_daily_bps=1,
            jitter_bps=1,
        )
        claim = freeze_claim(session)
        ResearchMemory(session).record(
            ResearchRecord(
                record_id="underpowered-1",
                kind=RecordKind.FINDING,
                subject="momentum ranking",
                statement="688 forecasts cannot resolve an IC of 0.15; 4,485 are needed.",
                verdict=Verdict.UNDERPOWERED,
                hypothesis_id=claim,
                hypothesis_version=1,
                source="H-2026-026",
                recorded_at=NOW,
            )
        )
        evidence = forward_evidence(
            session, hypothesis_id=claim, max_drawdown_limit_bps=2000
        )

    assert evidence.role is DeploymentRole.EDGE_CANDIDATE
    assert evidence.supported, evidence.explain()


def test_a_deployment_with_no_registration_is_an_operational_baseline(
    ledger_db: Database,
) -> None:
    """The role is derived from durable state, so nobody has to remember to set it."""
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        seed_prices(
            session, days=MINIMUM_FORWARD_DAYS, strategy_daily_bps=400, benchmark_daily_bps=1
        )
        evidence = forward_evidence(session)

    assert evidence.role is DeploymentRole.OPERATIONAL_BASELINE
    assert "OPERATIONAL_BASELINE" in evidence.explain()


def test_the_refutation_trigger_follows_lineage_rather_than_prose(ledger_db: Database) -> None:
    """A refutation of a *different* hypothesis must not block this one.

    #53 asks for the trigger to be registered lineage rather than text matching against
    REFUTED.md, and the two disagree in both directions: a strategy id appearing in unrelated
    prose would block wrongly, and a refutation phrased without the id would fail to block --
    silently, which is the worse half.
    """
    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        seed_prices(
            session,
            days=MINIMUM_FORWARD_DAYS,
            strategy_daily_bps=120,
            benchmark_daily_bps=1,
            jitter_bps=1,
        )
        claim = freeze_claim(session)
        _refute(session, "H-2026-999-some-other-question")
        evidence = forward_evidence(
            session, hypothesis_id=claim, max_drawdown_limit_bps=2000
        )

    assert evidence.role is DeploymentRole.EDGE_CANDIDATE
    assert evidence.supported, evidence.explain()
