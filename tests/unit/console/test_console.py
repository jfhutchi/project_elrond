"""The operator console, and the ways a polished surface misleads the person reading it (#82).

A console for an unproven research system is mechanically a machine for making one look like a
product. Most of these tests are therefore about what the page must *not* imply: that an unrun
check passed, that a venue feed sees the whole market, that an absent measurement is a zero.

The rest are about the boundary. Slice 2 is read-only, and "read-only" is asserted against the
route table rather than trusted to reviewers noticing a new POST.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quantbot.console.app import NAVIGATION, create_app
from quantbot.storage import Database, StorageRepository

SECRET_KEY = "pk-do-not-print"
SECRET_ACCOUNT = "acct-0123456789-should-never-render"


@pytest.fixture
def database(tmp_path: Path) -> Database:
    store = Database.create(tmp_path / "console.db")
    yield store
    store.close()


@pytest.fixture
def client(database: Database, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("EXPECTED_ACCOUNT_ID", SECRET_ACCOUNT)
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", SECRET_KEY)
    monkeypatch.setenv("ALPACA_PAPER_API_SECRET", "sk-do-not-print")
    return TestClient(create_app(database))


def everything(client: TestClient) -> str:
    """Every rendered byte the console can produce, pages and API together."""
    return "".join(client.get(href).text for href, _ in NAVIGATION) + client.get(
        "/api/state"
    ).text


# --- the boundary --------------------------------------------------------------------------


#: The complete set of routes on this console that may change anything (#82 slice 6). An
#: allowlist rather than a blanket "no mutation" assertion, because the console now has a control
#: plane -- but the property the old assertion protected is kept exactly: a POST added anywhere
#: else fails the suite rather than being noticed in review.
MUTATING_ROUTES = {
    ("/config/policy/preview", "POST"),
    ("/config/policy/apply", "POST"),
    ("/config/policy/rollback", "POST"),
    # The kill switch. Engaging is ungated -- stopping is never the dangerous direction.
    # Clearing cannot be faked from here: `KillSwitchController.clear` accepts only a
    # `MeasuredReadiness`, and the token that makes one is private to the module that owns the
    # gate. This surface can ask whether it is safe to resume and relay the answer; it cannot
    # assert it.
    ("/kill-switch/engage", "POST"),
    ("/kill-switch/clear", "POST"),
    # Reconcile. Safe in the specific sense that matters: it submits no order, cancels nothing
    # and moves no position. It reads the broker, adopts its authoritative fills and records
    # what agrees -- and it reaches the broker through a narrow injected callable rather than a
    # `Runtime`, so this process still cannot reach the order path at all.
    ("/operations/reconcile", "POST"),
    # Sync market data. Safe in the same sense, and with one guarantee reconcile does not have:
    # `save_bars` refuses to revise a bar it already holds, so a repeated sync cannot rewrite
    # history. It reaches the provider through a narrow injected callable whose object graph --
    # database, bar source, calendar, config, frozen veto policy -- contains no broker and no
    # execution coordinator.
    #
    # Added deliberately, which is what this list is for: the whole-suite run caught it and this
    # edit is the review moment.
    ("/operations/sync-data", "POST"),
}


def test_the_console_mutates_only_through_the_policy_control_plane(client: TestClient) -> None:
    """Every mutating route is named here, and nothing else may mutate.

    Slice 2 asserted the console could not change anything at all. Slice 6 gives it exactly one
    thing it may change -- the veto policy, through validate/diff/apply -- so the assertion moves
    from "none" to "these three", which is still checkable and still fails closed. Widening it
    requires editing this list, which is the review moment the old test was buying.
    """
    routes = {
        (route.path, method)
        for route in client.app.routes
        for method in getattr(route, "methods", set())
    }
    mutating = {item for item in routes if item[1] in {"POST", "PUT", "PATCH", "DELETE"}}

    assert mutating == MUTATING_ROUTES, (
        f"unexpected mutating routes: {sorted(mutating - MUTATING_ROUTES)}; "
        f"missing: {sorted(MUTATING_ROUTES - mutating)}"
    )


def test_nothing_on_the_console_can_reach_the_capital_boundary(client: TestClient) -> None:
    """The property that actually matters, asserted end to end rather than trusted to the model.

    `proposable_fields` excludes credentials and the capital boundary by safety class, and unit
    tests cover that. This checks the *route* honours it: a request naming `KILL_SWITCH` is
    refused rather than silently dropped from the update set, because a silently dropped field
    would apply a change set that is not the one submitted and report success.
    """
    for field in ("KILL_SWITCH", "TRADING_MODE", "ALPACA_PAPER_API_KEY", "authority"):
        response = client.post(
            "/config/policy/preview", data={field: "x", "reason": "attempting the boundary"}
        )

        assert response.status_code == 200
        assert "cannot be changed from a console" in response.text, field
        assert "Proposed change" not in response.text, f"{field} produced a diff"


def test_every_navigation_target_renders(client: TestClient) -> None:
    """A link in the shell that 404s is a console that lies about what it offers."""
    for href, label in NAVIGATION:
        response = client.get(href)
        assert response.status_code == 200, f"{href} ({label}) returned {response.status_code}"
        assert label in response.text or href == "/"


# --- what must never reach the browser -----------------------------------------------------


def test_no_credential_or_account_identifier_is_ever_rendered(client: TestClient) -> None:
    """Asserted against the actual secret strings, not against a redaction marker.

    A test that only looked for `[redacted]` would pass a page that printed both. The account id
    is included because this repository is public and the console feeds exports: whether an
    account is configured is useful, which account it is is not.
    """
    body = everything(client)

    assert SECRET_KEY not in body
    assert "sk-do-not-print" not in body
    assert SECRET_ACCOUNT not in body
    # And the registry's own markers are what appear instead.
    assert "[redacted]" in body
    assert "[configured]" in body


# --- absence must not read as success ------------------------------------------------------


def test_an_unrun_reconciliation_does_not_render_as_a_clean_one(client: TestClient) -> None:
    """"Never run" and "ran and found nothing" are different facts about the broker."""
    page = client.get("/").text

    assert "never run" in page
    assert "an unrun check is not a passed one" in page
    assert client.get("/api/state").json()["health"]["reconciliation_clean"] is None


def test_an_unmeasured_capacity_verdict_is_not_a_passing_check(client: TestClient) -> None:
    """#51's whole point: `UNASSESSED` is a fact about the store, not a green tick."""
    page = client.get("/capacity").text

    assert "UNASSESSED" in page
    assert "not a passing check" in page
    assert "absent" in page, "absence must be styled as absence rather than as a neutral value"
    assert client.get("/api/state").json()["capacity"]["verdict"] is None


def test_an_absent_equity_is_not_rendered_as_zero(client: TestClient) -> None:
    """A dash in a value slot that reads as $0 is a wrong number, not a missing one."""
    page = client.get("/").text

    assert "not snapshotted" in page
    assert "this is not $0" in page


# --- danger state --------------------------------------------------------------------------


def test_an_engaged_kill_switch_is_the_first_thing_on_every_page(client: TestClient) -> None:
    """A layout that let an operational emergency scroll would be a design decision about one."""
    for href, _ in NAVIGATION:
        page = client.get(href).text
        # The applied class, not the stylesheet rule of the same name -- that appears on every
        # page whatever the state, so matching it would assert nothing.
        assert 'class="strip strip--danger"' in page, f"{href} does not flag the engaged switch"
        strip = page.index('class="strip strip--danger"')
        assert strip < page.index("<main>"), f"{href} renders the danger strip after content"
        assert "ENGAGED" in page


def test_a_cleared_kill_switch_drops_the_danger_styling(
    database: Database, client: TestClient
) -> None:
    """The strip must distinguish states, or it is decoration rather than a signal."""
    from quantbot.operations.kill_switch import KillSwitchController
    from tests.support import measured

    KillSwitchController(database).clear(
        reason="test fixture readiness", measured=measured(), updated_at=datetime.now(UTC)
    )

    page = client.get("/").text
    assert 'class="strip strip--danger"' not in page
    assert "CLEAR" in page


# --- limited feed semantics ----------------------------------------------------------------


def test_a_venue_feed_is_never_presented_as_full_market_volume(client: TestClient) -> None:
    """#82 names this explicitly, and #56 measured what ignoring it costs.

    IEX volume-shock triggers scored 36.6% precision against the consolidated tape. A console
    that let `SINGLE_VENUE` read as market-wide would repeat the defect the detector refuses.
    """
    page = client.get("/market-data").text
    state = client.get("/api/state").json()["market_data"]

    assert state["volume_coverage"] == "SINGLE_VENUE"
    assert state["supports_volume_triggers"] is False
    assert "sample of consolidated volume" in page
    assert "36.6% precision" in page


# --- one source of truth -------------------------------------------------------------------


def test_the_api_and_the_pages_read_the_same_state(
    client: TestClient, database: Database
) -> None:
    """A page that computed something the API did not would be a second implementation of truth."""
    from quantbot.research.promotion import Stage
    from quantbot.storage.repositories import PromotionRecord

    with database.transaction() as session:
        StorageRepository(session).save_promotion(
            PromotionRecord(
                strategy_id="adaptive-momentum",
                stage=Stage.CANDIDATE.value,
                strategy_version="1.0.0",
                configuration_hash="cfg-abc",
                reason="seeded for the console test",
                actor="test",
                updated_at=datetime.now(UTC),
            ),
            direction="PROMOTION",
        )

    state = client.get("/api/state").json()
    page = client.get("/").text

    assert state["strategies"], "the fixture must produce a strategy or this proves nothing"
    for row in state["strategies"]:
        assert row["strategy_id"] in page
        assert row["stage"] in page


def test_the_risk_panel_reports_the_frozen_policy_rather_than_a_copy(client: TestClient) -> None:
    """The identity is what lets a refusal be traced to the rule that produced it."""
    from quantbot.risk.vetoes import PortfolioVetoPolicy

    policy = PortfolioVetoPolicy()
    state = client.get("/api/state").json()["risk"]

    assert state["policy_identity"] == policy.identity
    assert state["authority"] == policy.authority.value
    assert set(state["enabled_vetoes"]) == {item.value for item in policy.enabled}
    assert state["unimplemented_vetoes"] == [], "a declared veto with no evaluator must surface"


def test_the_attention_list_names_what_is_unmeasured(client: TestClient) -> None:
    """Blockers first, routine activity not at all.

    A research loop generates a large volume of uninteresting activity by design, and a panel
    that showed throughput without its denominator would read as momentum.
    """
    page = client.get("/").text

    assert "kill switch ENGAGED" in page
    assert "no capacity assessment has been persisted" in page
    assert "volume-derived anomaly detection is unavailable" in page


def test_a_store_with_no_kill_switch_row_reads_as_engaged(
    database: Database, client: TestClient
) -> None:
    """The fail-closed branch, reached deliberately because nothing else reaches it.

    `Database.create` always seeds the singleton, so this state only occurs in a damaged store --
    which is precisely when the answer matters. The safe reading of a missing safety control is
    that it is engaged, never the reverse, and without this test that branch was an assertion
    nobody had checked.
    """
    with database.engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM kill_switch_state")

    state = client.get("/api/state").json()["health"]
    page = client.get("/").text

    assert state["kill_switch_engaged"] is True
    assert "no kill-switch state is recorded" in state["kill_switch_reason"]
    assert 'class="strip strip--danger"' in page


def test_the_danger_strip_does_not_claim_the_console_is_read_only(client: TestClient) -> None:
    """It said "read-only console -- nothing here can change state" on every page.

    That was true until #121 added the policy control plane, and then it was a standing false
    claim on the one surface an operator reads to know what this thing can do. Caught by looking
    at the rendered page; every test here passed while it was wrong.

    What replaces it is the claim that is still true and is the one that matters.
    """
    body = client.get("/").text

    assert "nothing here can change state" not in body
    assert "no surface here can enable live trading or move capital" in body


def test_a_stopped_system_says_so_rather_than_showing_a_date(database, monkeypatch) -> None:
    """The gap the real outage exposed.

    The operator's system stopped on 2026-08-19 and was still stopped ten days later. The console
    rendered `last_run_at` as a timestamp and nothing else, so noticing required subtracting one
    date from another -- and nobody did. Meanwhile capacity, counterfactuals and reproduction
    were all recorded as "evidence blocked", which was true and pointed away from the cause.

    Elapsed time now leads, and it reaches the attention list ahead of every other item, because
    a system that is not running makes every other measurement on the page a statement about the
    past.
    """
    from datetime import UTC, datetime, timedelta

    from quantbot.console.state import read_console_state

    stopped = datetime(2026, 8, 19, 20, 5, tzinfo=UTC)
    with database.transaction() as session:
        StorageRepository(session).create_run(
            "run-1", strategy_identity(), started_at=stopped
        )

    with database.transaction() as session:
        state = read_console_state(session, now=stopped + timedelta(days=10))

    assert state.health.days_since_last_run == 10
    assert any("10 days ago" in item for item in state.attention)
    assert "no forward evidence" in " ".join(state.attention)


def test_a_recent_cycle_raises_nothing(database) -> None:
    """A notice that fires on a healthy system is one an operator learns to scroll past."""
    from datetime import UTC, datetime, timedelta

    from quantbot.console.state import read_console_state

    ran = datetime(2026, 8, 29, 20, 5, tzinfo=UTC)
    with database.transaction() as session:
        StorageRepository(session).create_run("run-1", strategy_identity(), started_at=ran)

    with database.transaction() as session:
        state = read_console_state(session, now=ran + timedelta(days=1))

    assert state.health.days_since_last_run == 1
    assert not [item for item in state.attention if "days ago" in item]


def test_a_system_that_never_ran_is_distinguished_from_a_stale_one(database) -> None:
    """Never-run and stopped are different facts, and only one of them is a fault."""
    from datetime import UTC, datetime

    from quantbot.console.state import read_console_state

    with database.transaction() as session:
        state = read_console_state(session, now=datetime(2026, 8, 30, tzinfo=UTC))

    assert state.health.days_since_last_run is None
    assert any("has ever run" in item for item in state.attention)


def _observe(database, at, outcome="HEALTHY", reasons="") -> None:
    """Write one heartbeat row directly, as the observer would."""
    from quantbot.storage.database import encode_utc
    from quantbot.storage.schema import session_observations

    with database.transaction() as session:
        session.execute(
            session_observations.insert().values(
                observation_id=f"observation-{encode_utc(at)}",
                observed_at=encode_utc(at),
                market_open=1,
                outcome=outcome,
                reasons=reasons,
                market_data_age_seconds=None,
                detail="{}",
            )
        )


def test_a_fresh_observation_says_the_daemon_is_alive(database) -> None:
    """A stale cycle with a fresh heartbeat is a daemon between sessions, not a stopped one.

    Without the heartbeat those two states are indistinguishable, which is why eleven days of a
    stopped system read as normal: `last_run_at` alone cannot tell "the cycle has not come round"
    from "nothing is running" (#143).
    """
    from datetime import UTC, datetime, timedelta

    from quantbot.console.state import read_console_state

    now = datetime(2026, 8, 30, 15, 0, tzinfo=UTC)
    _observe(database, now - timedelta(minutes=10))

    with database.transaction() as session:
        state = read_console_state(session, now=now)

    assert state.health.minutes_since_last_observation == 10
    assert state.health.last_observation_outcome == "HEALTHY"
    assert not [item for item in state.attention if "not running" in item]


def test_a_silent_loop_is_called_out(database) -> None:
    """Three missed passes is the smallest gap that cannot be one slow call or a restart."""
    from datetime import UTC, datetime, timedelta

    from quantbot.console.state import read_console_state

    now = datetime(2026, 8, 30, 15, 0, tzinfo=UTC)
    _observe(database, now - timedelta(hours=4))

    with database.transaction() as session:
        state = read_console_state(session, now=now)

    assert state.health.minutes_since_last_observation == 240
    assert any("probably" in item and "not running" in item for item in state.attention)


def test_a_store_that_has_never_observed_raises_no_daemon_notice(database) -> None:
    """A store upgraded a minute ago has no heartbeats, and that is not evidence of a fault.

    "Nothing has ever run" is already reported by the cycle line. Repeating it as a stopped
    daemon would fire on every fresh install, which is how a notice becomes noise.
    """
    from datetime import UTC, datetime

    from quantbot.console.state import read_console_state

    with database.transaction() as session:
        state = read_console_state(session, now=datetime(2026, 8, 30, tzinfo=UTC))

    assert state.health.minutes_since_last_observation is None
    assert not [item for item in state.attention if "not running" in item]


def strategy_identity():
    """The deployment a run has to hang off. `create_run` saves it as a side effect."""
    from datetime import UTC, datetime

    from quantbot.domain import StrategyIdentity

    return StrategyIdentity(
        strategy_id="adaptive-momentum",
        version="v1",
        git_commit="abc123",
        configuration_hash="cfg-abc",
        deployment_timestamp=datetime(2026, 8, 1, tzinfo=UTC),
    )


def test_the_console_is_given_a_callable_rather_than_a_runtime() -> None:
    """The design decision behind the reconcile action, asserted rather than described.

    `_reconcile` in `runtime.py` takes the whole object graph -- broker, execution coordinator,
    strategy runner. Handing that to a web process would put the order path inside it, and
    "no route calls it" is a far weaker guarantee than "this process cannot".

    Also: `build_runtime` writes. `load_or_create_identity` records a deployment and enters it on
    the promotion ladder, so building one at console start-up would mint deployment state because
    somebody opened a browser tab.
    """
    import ast
    from pathlib import Path as _Path

    tree = ast.parse(_Path("src/quantbot/cli.py").read_text(encoding="utf-8"))
    seam = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_console_reconcile"
    )
    built = {
        node.func.id
        for node in ast.walk(seam)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "LedgerSyncingRecovery" in built, "the seam builds recovery directly"
    assert "build_runtime" not in built, "a Runtime would carry the execution path"
    assert "load_or_create_identity" not in built, "start-up must not mint deployment state"


def test_a_console_without_a_broker_refuses_to_reconcile(database) -> None:
    """The form is hidden when it cannot work, and the route refuses anyway.

    Anything can POST to a local port, so a hidden button is a convenience and the refusal is
    the control -- the same reasoning as the kill switch.
    """
    from quantbot.console.app import create_app

    response = TestClient(create_app(database, reconcile=None)).post(
        "/operations/reconcile", follow_redirects=False
    )

    assert response.status_code == 200
    assert "cannot reconcile" in response.text
    assert "Nothing changed" in response.text


def test_a_failed_reconciliation_is_reported_rather_than_swallowed(database) -> None:
    """A redirect to a page showing unchanged state is indistinguishable from success.

    The same trap the policy apply path avoids, and the reason this renders in place.
    """
    from quantbot.console.app import create_app

    async def explode():
        raise RuntimeError("the broker refused the connection")

    response = TestClient(create_app(database, reconcile=explode)).post(
        "/operations/reconcile", follow_redirects=False
    )

    assert response.status_code == 200
    assert "reconciliation failed" in response.text
    assert "refused the connection" in response.text


def test_a_successful_reconciliation_redirects(database) -> None:
    """So a refresh does not repeat a broker round trip."""
    from quantbot.console.app import create_app

    called: list[bool] = []

    async def succeed():
        called.append(True)
        return {"ok": True, "status": "RECONCILED", "diff_count": 0, "reasons": []}

    response = TestClient(create_app(database, reconcile=succeed)).post(
        "/operations/reconcile", follow_redirects=False
    )

    assert response.status_code == 303
    assert called == [True]
