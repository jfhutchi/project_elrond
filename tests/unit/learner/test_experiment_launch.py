"""Pinned shadow launch cannot silently activate trading or broaden providers."""

from importlib import import_module
from pathlib import Path

import pytest

from tests.fixtures.adaptive_paper import manifest


def module():
    return import_module("quantbot.learner.experiment_launch")


def config_data(tmp_path):
    return dict(
        root=tmp_path / "shadow",
        store=tmp_path / "shadow" / "experiment.db",
        manifest_path=tmp_path / "manifest.json",
        operations_database=tmp_path / "operations.db",
        declaration_database=tmp_path / "declarations.db",
        clock_receipt=tmp_path / "clock.json",
        code_commit="a" * 40,
        manifest_hash=module().content_id(manifest()),
        mode="SHADOW",
        trading_enabled=False,
        maximum_workers=2,
    )


def test_config_cannot_reenable_entries_or_change_worker_budget(tmp_path):
    base = config_data(tmp_path)
    config = module().ExperimentLaunchConfig(**base)
    assert config.mode == "SHADOW"
    for changes in ({"trading_enabled": True}, {"mode": "LIVE"}, {"maximum_workers": 3}):
        with pytest.raises(ValueError):
            module().ExperimentLaunchConfig(**(base | changes))


def test_output_cannot_overlap_authoritative_sources(tmp_path):
    base = config_data(tmp_path)
    with pytest.raises(ValueError):
        module().ExperimentLaunchConfig(**(base | {"operations_database": base["store"]}))
    with pytest.raises(ValueError):
        module().ExperimentLaunchConfig(**(base | {"root": Path("relative")}))


def test_manifest_pin_and_synthetic_real_boundary_checked(tmp_path):
    base = config_data(tmp_path)
    base["manifest_path"].write_text(manifest().model_dump_json())
    config = module().ExperimentLaunchConfig(**base)
    with pytest.raises(ValueError, match="SYNTHETIC_NOT_DEPLOYABLE"):
        module().load_manifest(config)
    config = config.model_copy(update={"manifest_hash": "b" * 64})
    with pytest.raises(ValueError, match="MANIFEST_PIN"):
        module().load_manifest(config)


@pytest.mark.parametrize(
    "failure,reason",
    [
        (ValueError("CLOCK_SYNC_REQUIRED"), "CLOCK_SYNC_REQUIRED"),
        (ValueError("private credential-like diagnostic"), "COORDINATOR_TICK_FAILED"),
        (TimeoutError(), "COORDINATOR_TICK_TIMEOUT"),
    ],
)
async def test_runtime_failure_is_bounded_visible_and_sanitized(
    tmp_path, monkeypatch, failure, reason
):
    launch = module()
    config = launch.ExperimentLaunchConfig(**config_data(tmp_path))
    monkeypatch.setattr(launch, "load_manifest", lambda config: manifest())
    monkeypatch.setattr(launch, "verify_checkout", lambda config: None)

    async def fail(config, settings):
        raise failure

    monkeypatch.setattr(launch, "_run_once", fail, raising=False)
    result = await launch.run_once(config, object())
    assert result["reason"] == reason
    assert result["status"] == (
        "CLOCK_SYNC_REQUIRED" if reason == "CLOCK_SYNC_REQUIRED" else "FAULTED"
    )
    assert result["clock_verified"] is False
    assert result["fits"] == result["intents"] == 0
    saved = (config.root / "receipt.json").read_text()
    assert "private credential" not in saved
    assert "SHADOW" in saved


async def test_cancellation_is_not_misreported_as_recoverable_failure(tmp_path, monkeypatch):
    import asyncio

    launch = module()
    config = launch.ExperimentLaunchConfig(**config_data(tmp_path))
    monkeypatch.setattr(launch, "load_manifest", lambda config: manifest())
    monkeypatch.setattr(launch, "verify_checkout", lambda config: None)

    async def cancel(config, settings):
        raise asyncio.CancelledError()

    monkeypatch.setattr(launch, "_run_once", cancel, raising=False)
    with pytest.raises(asyncio.CancelledError):
        await launch.run_once(config, object())
    assert not (config.root / "receipt.json").exists()
