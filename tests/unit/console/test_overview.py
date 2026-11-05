"""Overview must distinguish unknown/flat inventory and keep account identity private."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.domain import Account, Position
from quantbot.storage import Database, StorageRepository


def core_role_fixture(database, now, **changes):
    from quantbot.paper_core.authority import RoleHeartbeat, RuntimeCommission
    from quantbot.storage.schema import metadata

    commission = RuntimeCommission(
        release_id="release",
        config_id="config",
        store_identity="store",
        store_path=str(database.path),
        account_id="private-account",
        owner="paper-core",
        generation=1,
        approved_at=now,
        approval_reference="fixture",
        initial_model_id="model",
        activation_policy="DAILY_BOUNDED_V3",
        learning_origin=now,
        legacy_owners=(),
        baseline_source_ids=("source",),
        canonical_metadata_id="a" * 64,
    )
    fields = dict(
        role="guardian",
        release_id="release",
        config_id="config",
        store_identity="store",
        owner="paper-core",
        generation=1,
        process_epoch="epoch",
        pid=123,
        recorded_at=now - timedelta(seconds=10),
        valid_until=now + timedelta(seconds=20),
        last_useful_progress=now - timedelta(seconds=60),
        objections=("TIME_EVIDENCE_EXPIRED",),
        first_blocker="TIME_EVIDENCE_EXPIRED",
        evidence_ids=(),
        proof_age_seconds=None,
    )
    fields.update(changes)
    heartbeat = RoleHeartbeat(**fields)
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


def test_current_core_role_exposes_safe_blocker_and_evidence_ages(tmp_path):
    from quantbot.console.overview import read_overview

    now = datetime(2026, 11, 4, 14, tzinfo=UTC)
    with Database.create(tmp_path / "console.db") as database:
        core_role_fixture(database, now)
        with database.transaction() as session:
            role = read_overview(session, account_id="private-account", now=now).roles[1]
    assert role.status == "Waiting"
    assert role.blocker_code == "TIME_EVIDENCE_EXPIRED"
    assert role.heartbeat_age_seconds == 10
    assert role.progress_age_seconds == 60
    assert role.last_useful_progress_at == now - timedelta(seconds=60)
    assert role.valid_until == now + timedelta(seconds=20)
    assert role.expired is False
    assert "time evidence" in role.reason.lower()
    assert role.action


@pytest.mark.parametrize("progress", [None, "future"])
def test_missing_or_future_progress_is_unknown_not_zero_age(tmp_path, progress):
    from quantbot.console.overview import read_overview

    now = datetime(2026, 11, 4, 14, tzinfo=UTC)
    with Database.create(tmp_path / "console.db") as database:
        core_role_fixture(
            database,
            now,
            first_blocker=None,
            objections=(),
            last_useful_progress=None if progress is None else now + timedelta(seconds=1),
        )
        with database.transaction() as session:
            panel = read_overview(session, account_id="private-account", now=now)
    assert panel.roles[1].progress_age_seconds is None
    assert panel.roles[1].blocker_code is None
    assert panel.roles[0].heartbeat_age_seconds is None
    assert panel.roles[0].valid_until is None


def test_expired_heartbeat_preserves_recorded_blocker_without_claiming_current_readiness(tmp_path):
    from quantbot.console.overview import read_overview

    now = datetime(2026, 11, 4, 14, tzinfo=UTC)
    with Database.create(tmp_path / "console.db") as database:
        core_role_fixture(
            database,
            now,
            recorded_at=now - timedelta(seconds=50),
            valid_until=now - timedelta(seconds=20),
            first_blocker="MARKET_CLOSED",
            objections=("MARKET_CLOSED",),
        )
        with database.transaction() as session:
            role = read_overview(session, account_id="private-account", now=now).roles[1]
    assert role.status == "Stale"
    assert role.expired is True
    assert role.heartbeat_age_seconds == 50
    assert role.blocker_code == "MARKET_CLOSED"


def test_missing_primary_blocker_uses_first_recorded_objection(tmp_path):
    from quantbot.console.overview import read_overview

    now = datetime.now(UTC)
    with Database.create(tmp_path / "console.db") as database:
        core_role_fixture(database, now, first_blocker=None)
        with database.transaction() as session:
            role = read_overview(session, account_id="private-account", now=now).roles[1]
    assert role.blocker_code == "TIME_EVIDENCE_EXPIRED"


@pytest.mark.parametrize(
    "blocker",
    [
        "<script>private-token</script>",
        "PRIVATE_API_SECRET",
        "CORE_MANUAL_ENTRY_PAUSE_EXTRA:private-token",
        "INVENTORY_PROTOCOL_INVALID:private-token",
    ],
)
def test_unknown_blocker_text_is_not_exposed_in_api_or_page(tmp_path, monkeypatch, blocker):
    monkeypatch.setenv("EXPECTED_ACCOUNT_ID", "private-account")
    with Database.create(tmp_path / "console.db") as database:
        core_role_fixture(database, datetime.now(UTC), first_blocker=blocker, objections=(blocker,))
        client = TestClient(create_app(database))
        response = client.get("/api/state")
        role = response.json()["overview"]["roles"][1]
        assert role["blocker_code"] == "UNRECOGNIZED_BLOCKER"
        assert blocker not in response.text
        assert blocker not in client.get("/").text


@pytest.mark.parametrize("primary", [True, False])
def test_manual_pause_suffix_is_private_but_code_remains_actionable(tmp_path, monkeypatch, primary):
    monkeypatch.setenv("EXPECTED_ACCOUNT_ID", "private-account")
    suffix = "<script>private-token</script>:reason"
    blocker = "CORE_MANUAL_ENTRY_PAUSE:" + suffix
    with Database.create(tmp_path / "console.db") as database:
        core_role_fixture(
            database,
            datetime.now(UTC),
            first_blocker=blocker if primary else None,
            objections=(blocker,),
        )
        client = TestClient(create_app(database))
        response = client.get("/api/state")
        role = response.json()["overview"]["roles"][1]
        assert role["blocker_code"] == "CORE_MANUAL_ENTRY_PAUSE"
        assert role["reason"] == "Manual entry pause is engaged"
        assert "audited resume" in role["action"]
        for document in (response.text, client.get("/").text):
            assert "private-token" not in document
            assert "CORE_MANUAL_ENTRY_PAUSE" in document


@pytest.mark.parametrize(
    "blocker",
    [
        "INVENTORY_ACQUISITION_RETRYABLE",
        "SESSION_OPENING_UNVERIFIED",
        "INVENTORY_PROTOCOL_INVALID",
        "CORE_ENTRY_FENCED",
    ],
)
def test_known_source_blocker_has_exact_code_and_static_next_step(tmp_path, blocker):
    from quantbot.console.overview import read_overview

    now = datetime.now(UTC)
    with Database.create(tmp_path / "console.db") as database:
        core_role_fixture(database, now, first_blocker=blocker, objections=(blocker,))
        with database.transaction() as session:
            role = read_overview(session, account_id="private-account", now=now).roles[1]
    assert role.blocker_code == blocker
    assert role.reason and role.action
    assert role.status == "Waiting"


def test_role_page_escapes_display_text_and_labels_evidence_times(tmp_path, monkeypatch):
    from quantbot.console import state
    from quantbot.console.overview import OverviewPanel, RoleStatus

    now = datetime.now(UTC)
    panel = OverviewPanel(
        core_phase="Commissioned",
        roles=(
            RoleStatus(
                role="guardian",
                status="Waiting",
                reason="<script>unsafe()</script>",
                blocker_code="TIME_EVIDENCE_EXPIRED",
                action="Inspect <receipt>",
                recorded_at=now,
                valid_until=now + timedelta(seconds=20),
                expired=False,
                heartbeat_age_seconds=10,
                last_useful_progress_at=now - timedelta(seconds=60),
                progress_age_seconds=60,
            ),
        ),
    )
    monkeypatch.setattr(state, "read_overview", lambda *args, **kwargs: panel)
    with Database.create(tmp_path / "console.db") as database:
        page = TestClient(create_app(database)).get("/").text
    assert "&lt;script&gt;unsafe()&lt;/script&gt;" in page
    assert "Inspect &lt;receipt&gt;" in page
    assert "<script>unsafe()" not in page
    assert "TIME_EVIDENCE_EXPIRED" in page
    assert "Heartbeat age" in page and "Last useful progress" in page
    assert "Expires" in page


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


@pytest.mark.parametrize(
    "release,bound,status",
    [
        ("release", False, "Waiting"),
        ("unapproved", False, "Unverified"),
        ("maintained", True, "Waiting"),
        ("release", True, "Unverified"),
    ],
)
def test_core_heartbeat_does_not_expose_identity_or_report_an_unapproved_release(
    tmp_path, monkeypatch, release, bound, status
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
    binding = None
    if bound:
        import hashlib

        from quantbot.paper_core.authority import ExecutableBinding
        from quantbot.paper_core.contracts import CoreConfig
        from quantbot.paper_core.release import PreparedRelease, RuntimeConfig

        config = RuntimeConfig(
            repository=str(tmp_path),
            store_path=str(tmp_path / "console.db"),
            store_identity="store",
            expected_account_hash=hashlib.sha256(account.encode()).hexdigest(),
            core=CoreConfig(
                account_id=account,
                symbol="AAPL",
                clock_profile="PAPER_TIME_V2",
                release_id="UNBOUND_PREPARED_RELEASE",
            ),
            holdout_source=str(tmp_path / "canonical.db"),
            learning_origin=now,
        )
        prepared = PreparedRelease(
            config=config,
            schema_version=36,
            executable_pins=(),
            interpreter=str(tmp_path / "python"),
            interpreter_sha256="a" * 64,
            python_version="fixture",
            distributions=(),
            base_runtime_pins=(),
        )
        binding = ExecutableBinding(
            commission_id=commission.id,
            sequence=2,
            previous_id="previous",
            release=prepared,
            approved_at=now,
            approval_reference="test-only",
            fence_id="fence",
            activation_id="activation",
            evidence_ids=("source",),
        )
        if release == "maintained":
            release = prepared.id
    heartbeat = RoleHeartbeat(
        role="guardian",
        release_id=release,
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
            if binding:
                session.execute(
                    metadata.tables["core_runtime_config_history"]
                    .insert()
                    .values(id=binding.id, payload=binding.model_dump_json())
                )
                session.execute(
                    metadata.tables["core_runtime_current"]
                    .insert()
                    .values(id="current", document_id=binding.id)
                )
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
        assert response.json()["overview"]["roles"][1]["status"] == status
        if status == "Unverified":
            role = response.json()["overview"]["roles"][1]
            assert role["blocker_code"] is None
            assert role["heartbeat_age_seconds"] is None
            assert role["progress_age_seconds"] is None
            assert role["valid_until"] is None
