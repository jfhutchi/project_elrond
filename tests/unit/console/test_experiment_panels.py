"""Read-only experiment views distinguish consent, setup and normal waiting."""

from datetime import timedelta
from importlib import import_module

from quantbot.learner.experiment_store import ExperimentStore
from tests.fixtures.adaptive_paper import manifest


def module():
    return import_module("quantbot.console.experiments")


def test_historical_fit_results_are_read_only_as_of_and_not_forward_profit(tmp_path):
    from quantbot.learner.experiment_research import FitReceipt

    frozen = manifest()
    path = tmp_path / "experiment.db"
    store = ExperimentStore(path, create=True)
    store.append(frozen, idempotency_key="manifest")
    for index, seconds in enumerate((0, 1, 60)):
        store.append(FitReceipt(
            experiment_id=frozen.experiment_id,
            recorded_at=frozen.registered_at + timedelta(seconds=seconds),
            request_id=str(index + 1) * 64, trial_id=f"synthetic-fit-{index}",
            status="SUCCEEDED", charged=True, candidate_id=str(index + 4) * 64,
            validation_mse=(index + 1) * 0.00001,
            reason="SYNTHETIC_TEST_RECEIPT", synthetic=True,
        ), idempotency_key=f"fit-{index}")
    before = path.read_bytes()
    panel = module().read_experiment_panel(
        path, experiment_id=frozen.experiment_id,
        now=frozen.registered_at + timedelta(seconds=2),
    )
    assert len(panel.detail.historical_fits) == 2
    assert [fit.validation_mse for fit in panel.detail.historical_fits] == [0.00001, 0.00002]
    assert panel.detail.fit_families == {}  # Missing model metadata stays unknown.
    assert panel.detail.evaluation.learner_realized_pnl is None
    assert panel.detail.summary.trading_authority == "NONE"
    assert path.read_bytes() == before


def test_missing_store_is_unavailable_and_never_created(tmp_path):
    path = tmp_path / "absent.db"
    panel = module().read_experiment_panel(path, now=manifest().registered_at)
    assert not panel.available
    assert panel.experiments == ()
    assert not path.exists()


def test_registration_has_development_owner_not_user_approval(tmp_path):
    path = tmp_path / "experiment.db"
    store = ExperimentStore(path, create=True)
    store.append(manifest(), idempotency_key="manifest")
    before = path.read_bytes()
    panel = module().read_experiment_panel(path, now=manifest().registered_at)
    assert panel.available
    summary = panel.experiments[0]
    assert summary.experiment_id == manifest().experiment_id
    assert summary.label == "Research coordinator not started"
    assert summary.owner == "DEVELOPMENT_OPERATOR"
    assert summary.blocker_kind == "SERVICE_SETUP"
    assert not summary.model_approval_required
    assert summary.trading_authority == "NONE"
    assert path.read_bytes() == before


def test_known_consent_is_setup_incomplete_not_another_model_choice(tmp_path):
    from quantbot.learner.experiment_store import ConsentNotice

    path = tmp_path / "experiment.db"
    store = ExperimentStore(path, create=True)
    frozen = manifest()
    store.append(frozen, idempotency_key="manifest")
    store.append(
        ConsentNotice(
            experiment_id=frozen.experiment_id,
            consent_id="operator-consent",
            recorded_at=frozen.registered_at,
        ),
        idempotency_key="notice",
    )
    # No fabricated ACTIVE_PAPER state or order authority.
    panel = module().read_experiment_panel(path, now=frozen.registered_at + timedelta(seconds=1))
    summary = panel.experiments[0]
    assert summary.consent_recorded
    assert summary.label == "Paper trading setup incomplete"
    assert not summary.model_approval_required
    assert summary.trading_authority == "NONE"
    assert "GPT" not in summary.next_action


def test_detail_keeps_synthetic_and_unknown_economics_explicit(tmp_path):
    path = tmp_path / "experiment.db"
    store = ExperimentStore(path, create=True)
    store.append(manifest(), idempotency_key="manifest")
    panel = module().read_experiment_panel(
        path, experiment_id=manifest().experiment_id, now=manifest().registered_at
    )
    assert panel.detail.synthetic
    assert panel.detail.evaluation.account_net_pnl is None
    assert panel.detail.evaluation.learner_realized_pnl is None
    assert panel.detail.manifest.roster == manifest().roster
    assert panel.detail.timeline[0].kind == "ExperimentManifest"


def test_worker_setup_and_failure_remain_visible_during_normal_closed_market(tmp_path):
    import json

    from quantbot.learner.experiment_coordinator import TickReceipt

    frozen = manifest()
    path = tmp_path / "experiment.db"
    store = ExperimentStore(path, create=True)
    store.append(frozen, idempotency_key="manifest")
    store.append(
        TickReceipt(
            experiment_id=frozen.experiment_id,
            recorded_at=frozen.registered_at,
            tick_id="a" * 64,
            input_hash="b" * 64,
            status="NORMAL_WAIT",
            reason="MARKET_CLOSED",
            next_expected_event="NEXT_EXCHANGE_SESSION",
            next_wakeup_at=frozen.registered_at + timedelta(minutes=2),
            clock_verified=True,
            synthetic=True,
        ),
        idempotency_key="closed-tick",
    )
    receipt = {
        "experiment_id": frozen.experiment_id,
        "recorded_at": frozen.registered_at.isoformat(),
        "status": "FAULTED",
        "reason": "RESEARCH_WORKER_FAILED",
        "code_commit": "c" * 40,
        "scope": "SHADOW",
        "synthetic": True,
    }
    worker_path = tmp_path / "worker-receipt.json"
    worker_path.write_text(json.dumps(receipt))
    result = module().read_experiment_panel(path, now=frozen.registered_at)
    assert result.experiments[0].label == "Research worker requires repair"
    assert result.experiments[0].blocker_kind == "FAILURE"
    receipt.update(status="NORMAL_WAIT", reason="PREDECLARED_RESEARCH_SOURCES_REQUIRED")
    worker_path.write_text(json.dumps(receipt))
    result = module().read_experiment_panel(path, now=frozen.registered_at)
    assert result.experiments[0].label == "Research input setup incomplete"
    assert not result.experiments[0].model_approval_required
