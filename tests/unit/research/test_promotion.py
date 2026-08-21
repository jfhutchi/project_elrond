"""What a strategy has to earn, and the step this system cannot take at all."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.domain import (
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
from quantbot.research.promotion import (
    ALLOWED_PROMOTIONS,
    MINIMUM_FORWARD_DAYS,
    ForwardObservation,
    LedgerObservations,
    PromotionLedger,
    PromotionRefused,
    PromotionState,
    Stage,
    count_forward_days,
    demote,
    forward_progress,
    material_change,
    observe_forward_days,
    promote,
    survivor_objections,
)
from quantbot.research.registry import DataRole
from quantbot.storage import Database, StorageRepository
from tests.unit.research.test_builder import (  # reuse the compiled plan and outcome fixtures
    outcome,
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

    with ledger_db.transaction() as session:
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        qualified = promote(
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
    assert qualified.stage is Stage.PAPER_QUALIFIED


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

        with pytest.raises(PromotionRefused, match="read from the durable ledger, not supplied"):
            ladder.promote(
                "adaptive-momentum",
                Stage.PAPER_QUALIFIED,
                actor="director",
                reason="I brought my own",
                now=NOW,
                observations=hand_built(MINIMUM_FORWARD_DAYS),
            )

        moved = ladder.promote(
            "adaptive-momentum",
            Stage.PAPER_QUALIFIED,
            actor="director",
            reason="the ledger agrees",
            now=NOW,
        )

    assert moved.stage is Stage.PAPER_QUALIFIED


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
