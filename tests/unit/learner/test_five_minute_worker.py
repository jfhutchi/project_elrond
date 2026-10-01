"""Native five-minute wiring uses synthetic isolated stores, never a real broker."""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from quantbot.learner import execution_worker as worker
from quantbot.learner.forward import _save
from quantbot.research.manifest import content_id
from tests.unit.learner.test_five_minute_paper import context as _paper_context
from tests.unit.learner.test_five_minute_paper import runtime_fixture

context = _paper_context


def publish(tmp_path, runner, monkeypatch):
    from quantbot.learner import five_minute_entry as entry
    from quantbot.learner.five_minute_launch import FiveMinuteLaunchConfig
    from quantbot.learner.five_minute_paper import FiveMinuteCohort

    base = runner._database.path.parent
    tmp_path.mkdir(exist_ok=True)
    source = base / "observer.json"
    cfg = FiveMinuteLaunchConfig(
        root=base / "observer",
        frozen=base / "frozen",
        operations_database=runner._database.path,
        declaration_database=base / "declarations.db",
        clock_receipt=base / "clock.json",
        code_commit="a" * 40,
        spec_hash="a" * 64,
        frozen_data_identity="b" * 64,
        mean_model=runner.model.model_id,
        ridge_model="f" * 64,
        mean_trial="intraday:dia-five-minute-" + "a" * 64,
        ridge_trial="intraday:dia-five-minute-" + "b" * 64,
    )
    source.write_text(cfg.model_dump_json())
    import hashlib

    payload = dict(
        model_id=runner.model.model_id,
        operational_database=str(runner._database.path),
        contract="five-minute-price-alpha-v1",
        experimental_symbol="DIA",
        policy=runner.policy.model_dump(mode="json"),
        five_minute_config=str(source),
        five_minute_config_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        expected_binding_hash=content_id(runner.binding),
    )
    (tmp_path / "paper-execution.json").write_text(json.dumps(payload))
    (tmp_path / "paper-consent.json").write_text(
        json.dumps(
            dict(
                schema="ELROND_PAPER_CONSENT_V1",
                mode="PAPER",
                model_id=runner.model.model_id,
                recorded_at=runner.predictions[0].recorded_at.isoformat(),
                authority="EXPLICIT_OPERATOR_CHAT_APPROVAL",
                maximum_new_positions=1,
                maximum_position_bps=500,
                maximum_entry_risk_bps=25,
                stop_bps=100,
                horizon_minutes=30,
                inherited_positions="MONITOR_ONLY_EXISTING_EXIT_OWNER",
            )
        )
    )
    monkeypatch.setattr(entry, "bound_models", lambda _: [runner._five_document])
    monkeypatch.setattr(entry, "clock_healthy", lambda *_: True)
    monkeypatch.setattr(
        entry,
        "load_five_minute_cohort",
        lambda *args, **kwargs: FiveMinuteCohort(
            runner._five_document, runner.predictions, runner._five_capture
        ),
    )
    _save(
        cfg.frozen / "preregistration.json",
        dict(training_start="2026-02-02", validation_end="2026-03-31"),
    )
    return entry, cfg, payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [
        "fresh",
        "waiting",
        "clock",
        "consent",
        "protected",
        "binding",
        "wrong_source",
        "missing_gate",
        "other_learner",
    ],
)
async def test_native_dispatch_is_exact_frozen_and_fail_closed(
    context, tmp_path, monkeypatch, scenario
):
    import quantbot.runtime
    from quantbot.learner import five_minute_paper
    from quantbot.operations.cycle import DataSyncResult

    database, broker, runner, supervisor = context
    directory = tmp_path / "entry-state"
    entry, _, payload = publish(directory, runner, monkeypatch)
    from quantbot.storage import Database

    (directory / "state").mkdir()
    learner = Database.create(directory / "state" / "learner.db")
    runtime = runtime_fixture(context, tmp_path)

    async def close():
        pass

    runtime.aclose = close
    monkeypatch.setattr(database, "close", lambda: None)
    monkeypatch.setattr(quantbot.runtime, "build_runtime", lambda _: runtime)
    monkeypatch.setattr(quantbot.runtime, "runtime_paths", lambda: runtime.paths)
    runtime.paths.database = database.path
    guards = []
    monkeypatch.setattr(
        entry,
        "_guard",
        lambda db, start, end: guards.append((start, end)) or scenario != "protected",
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return broker.now

    monkeypatch.setattr(entry, "datetime", Clock)
    if scenario == "waiting":
        monkeypatch.setattr(entry, "load_five_minute_cohort", lambda *a, **k: None)
    if scenario == "clock":
        monkeypatch.setattr(entry, "clock_healthy", lambda *a: False)
    if scenario == "consent":
        (directory / "paper-consent.json").unlink()
    if scenario == "binding":
        payload["expected_binding_hash"] = "0" * 64
    if scenario == "wrong_source":
        payload["five_minute_config_sha256"] = "0" * 64
    (directory / "paper-execution.json").write_text(json.dumps(payload))
    if scenario == "missing_gate":
        from quantbot.research.promotion import PromotionLedger, Stage

        with database.transaction() as session:
            PromotionLedger(session).demote(
                runner._identity.strategy_id,
                Stage.SHADOW,
                actor="test",
                reason="removed",
                now=broker.now,
            )
    if scenario == "other_learner":
        from quantbot.research.promotion import PromotionLedger, PromotionState, Stage

        with database.transaction() as session:
            PromotionLedger(session).enter(
                PromotionState(
                    strategy_id="learner-existing-one-minute",
                    strategy_version="1",
                    configuration_hash="a" * 64,
                    stage=Stage.PAPER_OBSERVATION,
                    reason="synthetic existing entry contract",
                    actor="test",
                    updated_at=broker.now,
                )
            )

    class Recovery:
        async def recover(self, *a, **k):
            return SimpleNamespace(ready=True)

    class RiskSync:
        async def sync(self):
            return DataSyncResult(
                healthy=True,
                completed_through=broker.now,
                bars_stored=0,
                stale_symbols=(),
                reasons=(),
            )

    runtime.recovery = Recovery()
    monkeypatch.setattr(five_minute_paper, "PilotRiskDataSync", lambda **k: RiskSync())
    await supervisor.run_once()
    if scenario == "wrong_source":
        with pytest.raises(ValueError, match="observer config"):
            await worker.execute_paper_tick(
                directory, learner, runtime.settings, code_commit="e" * 40
            )
        assert not broker.submitted
        return
    result = await worker.execute_paper_tick(
        directory, learner, runtime.settings, code_commit="e" * 40
    )
    if scenario == "fresh":
        assert result["orders_submitted"] == 1, result
        assert len(broker.submitted) == 1
        assert len(guards) >= 3
        # Repeated native invocation may not duplicate the handoff/submission.
        again = await worker.execute_paper_tick(
            directory, learner, runtime.settings, code_commit="e" * 40
        )
        assert again["orders_submitted"] == 0 and len(broker.submitted) == 1
    else:
        assert not broker.submitted
        if scenario == "other_learner":
            assert result["orders_submitted"] == 0
            assert "PAPER_OTHER_LEARNER_ENTRY_ACTIVE" in result["blockers"]
        else:
            assert result["status"] == ("WAITING" if scenario == "waiting" else "BLOCKED")
    if scenario in ("fresh", "waiting"):
        from quantbot.learner.receipts import valid_worker_authority

        receipt = dict(
            paper=result,
            promotion=result["promotion"],
            trading_enabled=result["trading_enabled"],
            code_commit="e" * 40,
        )
        assert valid_worker_authority(directory, receipt)
        receipt["code_commit"] = "f" * 40
        assert not valid_worker_authority(directory, receipt)
        receipt["code_commit"] = "e" * 40
        (directory / "paper-consent.json").unlink()
        assert not valid_worker_authority(directory, receipt)


def test_contract_config_cannot_infer_five_minute_or_weaken_limits(tmp_path):
    from pydantic import ValidationError

    basic = dict(model_id="a" * 64, operational_database=tmp_path / "paper.db")
    assert worker.PaperWorkerConfig(**basic).contract == "one-minute-price-alpha-v1"
    with pytest.raises(ValidationError):
        worker.PaperWorkerConfig(**basic, contract="five-minute-price-alpha-v1")
    with pytest.raises(ValidationError):
        worker.PaperWorkerConfig(**basic, five_minute_config=tmp_path / "observer.json")


@pytest.mark.asyncio
async def test_native_check_is_readonly_and_enforces_pin(context, tmp_path, monkeypatch, capsys):
    import quantbot.runtime
    from quantbot.storage import Database

    _, broker, runner, _ = context
    directory = tmp_path / "entry-state"
    entry, _, _ = publish(directory, runner, monkeypatch)
    (directory / "state").mkdir()
    Database.create(directory / "state" / "learner.db").close()
    env = tmp_path / "synthetic.env"
    env.write_text("TRADING_MODE=PAPER\n")
    monkeypatch.setattr(entry, "Settings", lambda: runtime_fixture(context, tmp_path).settings)
    monkeypatch.setattr(
        entry, "_git", lambda repo, *args: "e" * 40 if args[0] == "rev-parse" else ""
    )
    monkeypatch.setattr(
        quantbot.runtime, "runtime_paths", lambda: SimpleNamespace(database=runner._database.path)
    )

    def forbidden(*a, **k):
        pytest.fail("native --check may not build runtime, capture, fit or submit")

    monkeypatch.setattr(quantbot.runtime, "build_runtime", forbidden)
    monkeypatch.setattr(entry, "execute_five_minute_tick", forbidden)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    argv = ["--directory", str(directory), "--env-file", str(env), "--commit", "e" * 40, "--check"]
    assert entry.main(argv) == 0
    assert "RUNTIME_ELIGIBILITY_NOT_CHECKED" in capsys.readouterr().out
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    argv[-2] = "f" * 40
    assert entry.main(argv) == 2 and not broker.submitted


@pytest.mark.asyncio
async def test_worker_five_path_never_captures_or_trains_one_minute(context, tmp_path, monkeypatch):
    import asyncio

    from quantbot.learner import forward, service
    from quantbot.learner.worker import run_cycle
    from quantbot.storage import Database

    _, _, runner, _ = context
    directory = tmp_path / "entry-state"
    entry, _, _ = publish(directory, runner, monkeypatch)
    (directory / "state").mkdir()
    Database.create(directory / "state" / "learner.db").close()
    calls = []

    async def tick(*a, **k):
        calls.append(k)
        return dict(
            status="WAITING",
            trading_enabled=False,
            orders_submitted=0,
            blockers=["PAPER_CURRENT_COHORT_UNAVAILABLE"],
        )

    def forbidden(*a, **k):
        pytest.fail("five-minute native consumer may not train/capture one-minute data")

    monkeypatch.setattr(worker, "execute_paper_tick", tick)
    monkeypatch.setattr(forward, "run_forward_cycle", forbidden)
    monkeypatch.setattr(forward, "_train_challengers", forbidden)
    monkeypatch.setattr(service, "evaluate", forbidden)
    result = await asyncio.to_thread(
        run_cycle,
        directory,
        code_commit="e" * 40,
        settings=runtime_fixture(context, tmp_path).settings,
    )
    assert len(calls) == 1 and result["paper"]["status"] == "WAITING"
    assert result["operator_consent"] == "RECORDED"
    assert (directory / "worker.json").is_file()
    assert not (directory / "intraday").exists()


def test_native_task_plan_is_least_privilege_and_within_freshness_window():
    from datetime import date
    from xml.etree import ElementTree as ET

    from quantbot.learner.five_minute_entry import task_xml

    document = task_xml(
        repository=r"D:\release",
        directory=r"D:\entry",
        env_file=r"D:\paper.env",
        host=r"D:\host.exe",
        log=r"D:\entry.log",
        user_id="existing-user",
        commit="e" * 40,
        start_day=date(2026, 10, 2),
    )
    root = ET.fromstring(document)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find("t:Principals/t:Principal/t:RunLevel", ns).text == "LeastPrivilege"
    assert root.find("t:Settings/t:WakeToRun", ns).text == "false"
    assert root.find("t:Triggers/t:CalendarTrigger/t:StartBoundary", ns).text.endswith("09:31:35")
    arguments = root.find("t:Actions/t:Exec/t:Arguments", ns).text
    assert "quantbot.learner.five_minute_entry" in arguments
    assert "--directory 'D:\\entry' --commit '" + "e" * 40 + "'" in arguments
    assert "five_minute_launch" not in arguments


def test_lineage_guard_keeps_march31_training_end_out_of_april_holdout(tmp_path, monkeypatch):
    from datetime import date

    from quantbot.learner import five_minute_entry as entry

    source = SimpleNamespace(
        frozen=tmp_path / "frozen",
        operations_database=tmp_path / "paper.db",
        declaration_database=tmp_path / "declarations.db",
    )
    _save(
        source.frozen / "preregistration.json",
        dict(training_start="2026-02-02", validation_end="2026-03-31"),
    )
    calls = []

    def guard(database, start, end):
        calls.append((start, end))
        return end.date() < date(2026, 4, 1)

    monkeypatch.setattr(entry, "_guard", guard)
    assert entry.lineage_allowed(None, source)
    assert calls[-1][1].date() == date(2026, 3, 31)
    # An artifact actually trained into April still cannot use this path.
    source.frozen = tmp_path / "invalid-frozen"
    _save(
        source.frozen / "preregistration.json",
        dict(training_start="2026-02-02", validation_end="2026-04-01"),
    )
    assert not entry.lineage_allowed(None, source)
