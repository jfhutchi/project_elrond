"""Guard-before-read canaries and truthful bars-only local execution assumptions."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest


@pytest.mark.parametrize("kind", ["protected", "forward"])
def test_canonical_guard_refuses_archive_before_open(tmp_path, kind):
    from tests.unit.research.test_registry import make_draft, register, window

    from quantbot.paper_core.local_simulation import guard_day
    from quantbot.research.registry import DataRole, HypothesisRegistry
    from quantbot.storage import Database

    day = date(2026, 10, 14)
    path = tmp_path / "canonical.db"
    with Database.create(path) as db:
        with db.transaction() as session:
            register(
                HypothesisRegistry(session),
                make_draft(
                    windows=(
                        window(DataRole.DISCOVERY, "2026-09-01", "2026-09-02"),
                        window(
                            DataRole.PROTECTED_EVALUATION
                            if kind == "protected"
                            else DataRole.FORWARD_PAPER,
                            day.isoformat(),
                            day.isoformat(),
                        ),
                    )
                ),
                now=datetime(2026, 10, 1, tzinfo=UTC),
            )
    before = path.read_bytes()
    with pytest.raises(ValueError, match="PROTECTED"):
        guard_day(path, path, day=day)
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_recorded_real_market_archives_local_simulation(tmp_path):
    from quantbot.paper_core.local_simulation import demonstrate_recorded_archives

    config = Path(
        "C:/Users/leepa/Documents/Codex/2026-09-30/task/exploration-commission-b496109/source.json"
    )
    index = (
        Path(__file__).parents[3] / ".superpowers/sdd/2026-10-13-paper-core/m5-archive-index.json"
    )
    if not config.is_file() or not index.is_file():
        pytest.skip("authorized local archive configuration or index unavailable on this host")
    evidence = await demonstrate_recorded_archives(
        tmp_path,
        source_config=config,
        index_path=index,
    )
    assert evidence["evidence_lane"] == "LOCAL_SIM_REAL_DATA"
    assert evidence["learning_receipt"]["status"] == "NO_CHANGE"
    assert evidence["learning_receipt"]["reason"] == "TEN_COMPLETED_SESSION_WARMUP"
    assert evidence["prospective_eligibility_claimed"] is False
    assert evidence["source_stores_read_only"] is True
    assert {s["scenario"] for s in evidence["scenarios"]} == {
        "base",
        "latency",
        "partial",
        "reject",
    }
    assert all(s["bundle"]["evidence_lane"] == "LOCAL_SIM_REAL_DATA" for s in evidence["scenarios"])
    import json

    print("M5_REAL_SIM:" + json.dumps(evidence, sort_keys=True))


def test_wrong_day_locator_is_refused_before_archive_hydration(tmp_path, monkeypatch):
    import json
    import sqlite3

    from tests.unit.learner.test_experiment_contract import manifest_data

    from quantbot.learner.experiment_contract import ExperimentManifest
    from quantbot.paper_core import local_simulation

    manifest = ExperimentManifest.model_validate(manifest_data())
    store = tmp_path / "experiment.db"
    with sqlite3.connect(store) as connection:
        connection.execute(
            "CREATE TABLE events (kind TEXT,experiment_id TEXT,document TEXT,"
            "idempotency_key TEXT,digest TEXT)"
        )
        connection.execute(
            "INSERT INTO events VALUES (?,?,?,?,?)",
            (
                "ExperimentManifest",
                manifest.experiment_id,
                manifest.model_dump_json(),
                "manifest",
                "digest",
            ),
        )
        connection.execute(
            "INSERT INTO events VALUES (?,?,?,?,?)",
            (
                "ObservationBatch",
                manifest.experiment_id,
                json.dumps(
                    {
                        "synthetic": False,
                        "features": [
                            {
                                "symbol": "SYN0",
                                "feature_at": "2026-10-13T14:05:00Z",
                                "target_at": "2026-10-13T14:35:00Z",
                                "receipt_at": "2026-10-13T14:06:00Z",
                                "source_snapshot_hash": "a" * 64,
                            }
                        ],
                    }
                ),
                "source-locator",
                "immutable-event-digest",
            ),
        )

    def forbidden(_):
        raise AssertionError("mismatched day reached archived prices/features")

    monkeypatch.setattr(local_simulation, "read_snapshot", forbidden)
    with pytest.raises(ValueError, match="PRE_READ_WINDOW_BINDING"):
        local_simulation.read_guarded_archive(
            path=tmp_path / ("a" * 64 + ".json"),
            expected_hash="a" * 64,
            symbol="SYN0",
            day=date(2026, 10, 14),
            manifest=manifest,
            operations=tmp_path / "never-open-operations",
            declarations=tmp_path / "never-open-declarations",
            experiment_store=store,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["stale", "missing_dependency"])
async def test_local_replay_stale_or_missing_dependency_abstains(tmp_path, kind):
    from datetime import timedelta

    from quantbot.paper_core.contracts import CoreConfig, ModelArtifact, Observation
    from quantbot.paper_core.local_simulation import LocalSimulationBroker
    from quantbot.paper_core.runtime import initialize_fixture
    from quantbot.paper_core.store import CoreStore
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.storage import Database

    now = datetime(2026, 10, 14, 14, 5, tzinfo=UTC)
    config = CoreConfig(
        account_id="local-input-canary", symbol="XLF", evidence_lane="LOCAL_SIM_REAL_DATA"
    )
    broker = LocalSimulationBroker(account_id=config.account_id, now=now)
    with Database.create(tmp_path / "core.db") as db:
        store = CoreStore(db, config)
        proof = initialize_fixture(store, broker, now=now, entry_enabled=True)
        end = now - timedelta(seconds=121) if kind == "stale" else now
        observation = Observation(
            recorded_at_utc=now,
            evidence_lane="LOCAL_SIM_REAL_DATA",
            feed="LOCAL_SIM_REAL_DATA",
            symbol="XLF",
            source_start=end - timedelta(minutes=35),
            source_end=end,
            available_at=now,
            price=100,
            raw_data_digest="local-input-canary",
            features=(("return", 0.01),),
            dependencies=("SPY",),
            dependency_available_at=(None if kind == "missing_dependency" else now,),
            sequence=1,
            session_close=now + timedelta(hours=5),
        )
        decision = await trader_cycle(store, broker, observation, ModelArtifact(), proof, now=now)
        assert decision.action == "WAIT"
        assert (
            "OBSERVATION_STALE" if kind == "stale" else "DEPENDENCY_UNAVAILABLE"
        ) in decision.gates
        assert not broker.posts
