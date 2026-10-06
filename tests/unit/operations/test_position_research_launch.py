import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from quantbot.config import Settings
from quantbot.market_data.instrument import InstrumentIdentity, SymbolMapping
from quantbot.market_data.instruments import Instrument
from quantbot.operations import position_research_launch as module
from quantbot.storage import Database, StorageRepository
from tests.unit.learner.test_paper_runner import context as _context


def configuration(at):
    targets = tuple(
        Instrument(
            instrument_id="id-" + symbol,
            symbol=symbol,
            asset_class="ETF",
            venue="NASDAQ",
            exposure_market="US equity",
            listed_on=at.date(),
        )
        for symbol in ("IWM", "QQQ")
    )
    identities = tuple(
        InstrumentIdentity(
            instrument_id=t.instrument_id,
            asset_class="ETF",
            current_symbol=t.symbol,
            status="RESOLVED",
            resolver="official-issuer-and-alpaca",
            resolved_at=at,
            source_refs=("https://issuer.example/verified",),
            mappings=(SymbolMapping(provider="alpaca", symbol=t.symbol, valid_from=at.date()),),
        )
        for t in targets
    )
    return module.ShadowConfiguration(
        targets=targets,
        identities=identities,
        model_id="frozen-model",
        trading_commit="a" * 40,
        cost_bps=10,
        configured_at=at,
    )


@pytest.mark.parametrize(
    "change", [dict(order_authority=True), dict(targets=()), dict(identities=())]
)
def test_configuration_cannot_expand_order_authority_or_drop_identity(change):
    at = datetime.now(UTC)
    with pytest.raises((ValueError, KeyError)):
        configuration(at).model_copy(update=change).verify(at)


def test_current_prediction_never_reads_other_commit_or_historical_day(tmp_path):
    at = datetime.now(UTC)
    (tmp_path / "worker.json").write_text(json.dumps(dict(code_commit="b" * 40)))
    assert module.current_prediction(tmp_path, configuration(at), "QQQ", at) is None


@pytest.fixture
def context(tmp_path, request):
    yield from _context.__wrapped__(tmp_path, request)


def test_forecast_reader_accepts_real_valid_cohort_and_rejects_stale_future_corruption(
    context, tmp_path
):
    from quantbot.learner.forward import _save

    _, broker, runner, _ = context
    at = broker.now
    prediction = next(p for p in runner.predictions if p.features.symbol == "QQQ")
    config = configuration(at).model_copy(update={"model_id": prediction.model_id})
    (tmp_path / "worker.json").write_text(
        json.dumps(
            dict(
                code_commit="a" * 40,
                finished_at=at.isoformat(),
                status="HEALTHY",
                promotion="PAPER_OBSERVATION",
                intraday=dict(predictions_this_run=1),
            )
        )
    )
    path = (
        tmp_path
        / "intraday"
        / "predictions"
        / at.date().isoformat()
        / ("valid-" + prediction.model_id + ".json")
    )
    _save(
        path, dict(model_id=prediction.model_id, predictions=[prediction.model_dump(mode="json")])
    )
    assert module.current_prediction(tmp_path, config, "QQQ", at) == prediction
    assert module.current_prediction(tmp_path, config, "QQQ", at + timedelta(minutes=6)) is None
    assert module.current_prediction(tmp_path, config, "QQQ", at - timedelta(seconds=1)) is None
    path.write_text(json.dumps(dict(hash="corrupt", document={})))
    assert module.current_prediction(tmp_path, config, "QQQ", at) is None
    (tmp_path / "worker.json").write_text(json.dumps(dict(code_commit="a" * 40)))
    assert module.current_prediction(tmp_path, configuration(at), "QQQ", at) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("position_failure", [False, True])
async def test_real_flow_uses_only_get_and_separate_shadow_store(
    tmp_path, monkeypatch, position_failure
):
    at = datetime.now(UTC) - timedelta(seconds=1)
    output, learner = tmp_path / "shadow", tmp_path / "learner"
    output.mkdir()
    learner.mkdir()
    db = Database.create(output / "shadow.db")
    db.close()
    (learner / "worker.json").write_text(json.dumps(dict(code_commit="a" * 40)))
    calls = []

    class Transport:
        async def request(self, method, url, *, headers, params, timeout_seconds):
            assert method == "GET"
            calls.append(url)
            if url.endswith("/positions"):
                payload, status = ([dict(symbol="QQQ")], 200) if not position_failure else ({}, 503)
            else:
                payload, status = (
                    {
                        "news": [
                            dict(
                                id=params["symbols"],
                                headline="SYNTHETIC news",
                                source="fixture",
                                created_at=at.isoformat(),
                                symbols=[params["symbols"]],
                            )
                        ]
                    },
                    200,
                )
            from types import SimpleNamespace

            return SimpleNamespace(status_code=status, content=json.dumps(payload).encode())

        async def aclose(self):
            pass

    monkeypatch.setattr(module, "HttpxMarketDataTransport", Transport)
    settings = Settings(ALPACA_PAPER_API_KEY="synthetic", ALPACA_PAPER_API_SECRET="synthetic")
    receipt = await module.run_once(output, learner, configuration(at), settings, "c" * 40)
    assert receipt["order_requests"] == receipt["operational_database_writes"] == 0
    assert len(calls) == 3 and all("/orders" not in c for c in calls)
    assert len(list(learner.iterdir())) == 1
    db = Database(output / "shadow.db")
    try:
        with db.transaction() as session:
            repo = StorageRepository(session)
            events = repo.list_signals()
            assert len(events) == 2 and not repo.list_unresolved_order_intents()
            for event in events:
                advice = event.payload["decision"]
                assert advice["forecast_id"] is None and advice["edge_bps"] is None
                assert advice["action"] == "WAIT" and advice["order_authority"] is False
                assert advice["reason"] == (
                    "POSITION_STATE_UNAVAILABLE" if position_failure else "FORECAST_UNAVAILABLE"
                )
    finally:
        db.close()


def test_paper_only_check_refuses_live_settings(tmp_path, monkeypatch):
    output, learner = tmp_path / "shadow", tmp_path / "learner"
    output.mkdir()
    learner.mkdir()
    environment = tmp_path / "test.env"
    environment.write_text("")
    (output / "config.json").write_text(configuration(datetime.now(UTC)).model_dump_json())
    monkeypatch.setattr(
        module, "_git", lambda _repo, *args: "c" * 40 if args[0] == "rev-parse" else ""
    )
    monkeypatch.setattr(
        module,
        "Settings",
        lambda: SimpleNamespace(
            TRADING_MODE=SimpleNamespace(value="LIVE"),
            BROKER_ENVIRONMENT=SimpleNamespace(value="LIVE"),
            LIVE_TRADING_ACKNOWLEDGED=True,
        ),
    )
    assert (
        module.main(
            [
                "--directory",
                str(output),
                "--learner-directory",
                str(learner),
                "--env-file",
                str(environment),
                "--commit",
                "c" * 40,
                "--check",
            ]
        )
        == 2
    )
    assert not (output / "shadow.db").exists() and not (output / "receipt.json").exists()
