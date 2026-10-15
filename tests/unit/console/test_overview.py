"""Overview must distinguish unknown/flat inventory and keep account identity private."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.domain import Account, Position
from quantbot.storage import Database, StorageRepository


def save(database, identity, account, positions, moment):
    with database.transaction() as session:
        StorageRepository(session).save_account_snapshot(
            identity,
            Account(
                account_id=account,
                cash=Decimal("100"),
                buying_power=Decimal("100"),
                equity=Decimal("200"),
                currency="USD",
            ),
            positions,
            captured_at=moment,
        )


def test_unknown_inventory_is_not_displayed_as_a_flat_account(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPECTED_ACCOUNT_ID", "private-primary-account")
    with Database.create(tmp_path / "console.db") as database:
        client = TestClient(create_app(database))
        panel = client.get("/api/state").json()["overview"]
        assert panel["positions"] is None
        assert panel["snapshot_at"] is None
        assert panel["core_phase"] == "Not commissioned"
        assert "private-primary-account" not in client.get("/").text


def test_inventory_uses_latest_configured_account_and_preserves_actual_marks(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPECTED_ACCOUNT_ID", "private-primary-account")
    moment = datetime(2026, 10, 15, 12, tzinfo=UTC)
    position = Position(
        symbol="MSFT",
        quantity=Decimal(".4"),
        market_price=Decimal("500"),
        market_value=Decimal("200"),
        average_entry_price=Decimal("480"),
    )
    with Database.create(tmp_path / "console.db") as database:
        save(database, "primary", "private-primary-account", (position,), moment)
        save(database, "other", "private-other-account", (), moment + timedelta(seconds=1))
        client = TestClient(create_app(database))
        panel = client.get("/api/state").json()["overview"]
        assert panel["snapshot_at"] == "2026-10-15T12:00:00Z"
        assert panel["positions"] == [
            {
                "symbol": "MSFT",
                "quantity": "0.4",
                "market_price": "500",
                "market_value": "200",
                "average_entry_price": "480",
            }
        ]
        page = client.get("/").text
        assert "MSFT" in page
        assert "private-primary-account" not in page
        assert "private-other-account" not in page
        save(database, "flat", "private-primary-account", (), moment + timedelta(seconds=2))
        assert client.get("/api/state").json()["overview"]["positions"] == []


def test_inventory_does_not_fall_back_to_an_unconfigured_account(tmp_path, monkeypatch):
    monkeypatch.delenv("EXPECTED_ACCOUNT_ID", raising=False)
    with Database.create(tmp_path / "console.db") as database:
        save(database, "other", "private-other-account", (), datetime.now(UTC))
        panel = TestClient(create_app(database)).get("/api/state").json()["overview"]
        assert panel["positions"] is None


def test_core_heartbeat_does_not_expose_account_identifiers_in_provider_errors(
    tmp_path, monkeypatch
):
    from quantbot.paper_core.authority import RoleHeartbeat, RuntimeCommission
    from quantbot.storage.schema import metadata

    account = "private-primary-account"
    monkeypatch.setenv("EXPECTED_ACCOUNT_ID", account)
    now = datetime.now(UTC)
    commission = RuntimeCommission(
        release_id="release",
        config_id="config",
        store_identity="store",
        store_path=str(tmp_path / "console.db"),
        account_id=account,
        owner="paper-core",
        generation=1,
        approved_at=now,
        approval_reference="test-only",
        initial_model_id="model",
        activation_policy="DAILY_BOUNDED_V3",
        learning_origin=now,
        legacy_owners=(),
        baseline_source_ids=("source",),
        canonical_metadata_id="a" * 64,
    )
    heartbeat = RoleHeartbeat(
        role="guardian",
        release_id="release",
        config_id="config",
        store_identity="store",
        owner="paper-core",
        generation=1,
        process_epoch="epoch",
        pid=123,
        recorded_at=now,
        valid_until=now + timedelta(seconds=30),
        last_useful_progress=now,
        objections=("PROVIDER_ERROR",),
        evidence_ids=(),
        proof_age_seconds=0,
        first_blocker="Provider validation error: " + account,
    )
    with Database.create(tmp_path / "console.db") as database:
        with database.transaction() as session:
            session.execute(
                metadata.tables["core_runtime_commissions"]
                .insert()
                .values(id=commission.id, payload=commission.model_dump_json())
            )
            session.execute(
                metadata.tables["core_role_heartbeats"]
                .insert()
                .values(id=heartbeat.id, payload=heartbeat.model_dump_json())
            )
            session.execute(
                metadata.tables["core_role_current"]
                .insert()
                .values(role="guardian", heartbeat_id=heartbeat.id)
            )
        client = TestClient(create_app(database))
        response = client.get("/api/state")
        assert account not in response.text
        assert account not in client.get("/").text
        assert response.json()["overview"]["roles"][1]["status"] == "Waiting"
