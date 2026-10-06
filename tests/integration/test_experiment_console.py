"""One truth for JSON and rendered HTML; no mutation/activation routes."""

from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.storage import Database
from tests.fixtures.adaptive_paper import manifest


def test_experiment_routes_are_read_only_and_share_panel(tmp_path, monkeypatch):
    from quantbot.console import app as console_app

    frozen = manifest()
    store = ExperimentStore(tmp_path / "experiment.db", create=True)
    store.append(frozen, idempotency_key="manifest")
    database = Database.create(tmp_path / "operations.db")
    # Freeze only the panel clock; no operational timestamps or P&L are invented.
    original = console_app.read_experiment_panel
    monkeypatch.setattr(
        console_app,
        "read_experiment_panel",
        lambda path, **kwargs: original(path, **(kwargs | {"now": frozen.registered_at})),
    )
    client = TestClient(create_app(database, experiment_store=store.path))
    api = client.get("/api/experiments")
    assert api.status_code == 200
    assert api.json()["experiments"][0]["experiment_id"] == frozen.experiment_id
    page = client.get("/experiments")
    assert page.status_code == 200
    assert "Research coordinator not started" in page.text
    assert "SYNTHETIC" in page.text
    detail = client.get("/experiments/" + frozen.experiment_id)
    assert detail.status_code == 200
    assert "Unknown" in detail.text
    assert "No live switch" in detail.text
    assert client.post("/experiments/" + frozen.experiment_id).status_code == 405
    assert client.get("/api/experiments/missing").status_code == 404
    assert store.state(frozen.experiment_id).state == "REGISTERED"
    for route in ("/", "/learner", "/research", "/calendar"):
        shared = client.get(route)
        assert shared.status_code == 200
        assert "Research coordinator not started" in shared.text
        assert "SYNTHETIC" in shared.text
    database.close()


def test_news_provenance_is_escaped_and_never_implies_execution_authority(tmp_path, monkeypatch):
    from quantbot.console import app as console_app
    from quantbot.learner.experiment_news import NewsEvidence

    frozen = manifest()
    store = ExperimentStore(tmp_path / "experiment.db", create=True)
    store.append(frozen, idempotency_key="manifest")
    identity = frozen.roster[0]
    store.append(
        NewsEvidence(
            experiment_id=frozen.experiment_id,
            symbol=identity.symbol,
            asset_id=identity.asset_id,
            recorded_at=frozen.registered_at,
            provider="synthetic-provider",
            kind="NEWS",
            source_url=None,
            source_hash=None,
            headline="<script>bad()</script>",
            retrieved_at=frozen.registered_at,
            published_at=None,
            first_seen_at=None,
            status="UNAVAILABLE",
            scope="FUND_LEVEL",
            synthetic=True,
        ),
        idempotency_key="synthetic-news",
    )
    database = Database.create(tmp_path / "operations.db")
    original = console_app.read_experiment_panel
    monkeypatch.setattr(
        console_app,
        "read_experiment_panel",
        lambda path, **kwargs: original(path, **(kwargs | {"now": frozen.registered_at})),
    )
    client = TestClient(create_app(database, experiment_store=store.path))
    page = client.get("/experiments/" + frozen.experiment_id)
    assert "&lt;script&gt;bad()&lt;/script&gt;" in page.text
    assert "<script>bad()</script>" not in page.text
    assert "FUND_LEVEL" in page.text and "UNAVAILABLE" in page.text
    database.close()
