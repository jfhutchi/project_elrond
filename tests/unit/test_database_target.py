"""Where the store lives, said without assuming a filesystem, and refusing what it cannot open.

The seam #196 asks for exists so #195 has somewhere to land. It enables nothing: a backend with
no host, no credentials and no proven schema parity must not become reachable by setting an
environment variable, and the way that happens by accident is a fallback -- point the system at
PostgreSQL, get SQLite, and watch a pristine empty store report itself healthy. That is #86's
failure arriving through the configuration instead of through a typo.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest

from quantbot.cli import main
from quantbot.storage import Database
from quantbot.storage.target import (
    PATH_VARIABLE,
    URL_VARIABLE,
    AmbiguousDatabaseTarget,
    BackendNotEnabled,
    DatabaseBackend,
    DatabaseTarget,
    DatabaseTargetError,
    UnsupportedDatabaseUrl,
    parse_database_url,
    redact_database_url,
    resolve_database_target,
)

SECRET = "hunter2-do-not-log-me"
POSTGRES_URL = f"postgresql://elrond:{SECRET}@db.internal:5432/quantbot"
QUERY_SECRET = "query-secret-do-not-log-me"
FRAGMENT_SECRET = "fragment-secret-do-not-log-me"
POSTGRES_QUERY_URL = (
    f"postgresql://elrond@db.internal:5432/quantbot?sslmode=require&password={QUERY_SECRET}"
    f"&access_token={QUERY_SECRET}#{FRAGMENT_SECRET}"
)


def test_an_unset_environment_means_exactly_what_it_always_meant() -> None:
    """Backward compatibility is the first acceptance criterion, so it is asserted first."""
    target = resolve_database_target({})

    assert target.backend is DatabaseBackend.SQLITE
    assert target.location == "quantbot.db"
    assert target.enabled is True
    assert target.path == Path("quantbot.db")


def test_the_path_variable_still_wins_on_its_own() -> None:
    target = resolve_database_target({PATH_VARIABLE: "/srv/elrond/quantbot.db"})

    assert target.backend is DatabaseBackend.SQLITE
    assert target.path == Path("/srv/elrond/quantbot.db")


def test_setting_both_a_path_and_a_url_is_refused_rather_than_ordered(
) -> None:
    """No precedence is invented, and the reason is not tidiness.

    A precedence rule means one of the two settings silently does nothing while the operator who
    set it believes it took effect -- which is how a system ends up writing to a store nobody
    meant to use, with both settings present and looking deliberate.
    """
    with pytest.raises(AmbiguousDatabaseTarget) as refused:
        resolve_database_target({PATH_VARIABLE: "a.db", URL_VARIABLE: POSTGRES_URL})

    assert refused.value.code == "DATABASE_TARGET_AMBIGUOUS"
    assert SECRET not in str(refused.value), "the refusal printed the password"


def test_a_postgresql_url_is_recognised_in_order_to_be_refused() -> None:
    """Recognised, not ignored. Silently falling back to SQLite is the failure mode."""
    target = resolve_database_target({URL_VARIABLE: POSTGRES_URL})

    assert target.backend is DatabaseBackend.POSTGRESQL
    assert target.enabled is False

    with pytest.raises(BackendNotEnabled) as refused:
        target.require_enabled()
    assert refused.value.code == "BACKEND_NOT_ENABLED"
    assert "#197" in str(refused.value), "the refusal names what would have to be true first"

    # And it has no filesystem path, rather than a plausible-looking one that fails later
    # somewhere less helpful.
    with pytest.raises(BackendNotEnabled):
        _ = target.path


def test_a_password_never_reaches_any_diagnostic() -> None:
    """Asserted against every renderer at once rather than trusting each call site.

    `repr` matters as much as `str` here and is the easier one to forget: a dataclass prints its
    fields verbatim, and a target reaches a log line or a traceback through `repr` far more often
    than through `str`.
    """
    target = resolve_database_target({URL_VARIABLE: POSTGRES_URL})

    renderings = [
        str(target),
        repr(target),
        target.display,
        redact_database_url(POSTGRES_URL),
    ]
    with pytest.raises(BackendNotEnabled) as refused:
        target.require_enabled()
    renderings.append(str(refused.value))
    with pytest.raises(BackendNotEnabled) as no_path:
        _ = target.path
    renderings.append(str(no_path.value))

    for rendering in renderings:
        assert SECRET not in rendering, f"a password leaked into {rendering!r}"
        assert "***" in rendering

    # Redaction that removed the host or the database name would make the diagnostic useless,
    # and neither is the secret.
    assert "db.internal" in target.display
    assert "quantbot" in target.display
    assert "elrond" in target.display


def test_query_credentials_and_fragments_never_reach_a_diagnostic() -> None:
    target = resolve_database_target({URL_VARIABLE: POSTGRES_QUERY_URL})

    renderings = [
        str(target),
        repr(target),
        target.display,
        redact_database_url(POSTGRES_QUERY_URL),
    ]
    with pytest.raises(BackendNotEnabled) as refused:
        target.require_enabled()
    renderings.append(str(refused.value))
    with pytest.raises(AmbiguousDatabaseTarget) as ambiguous:
        resolve_database_target(
            {PATH_VARIABLE: POSTGRES_QUERY_URL, URL_VARIABLE: POSTGRES_URL}
        )
    renderings.append(str(ambiguous.value))

    for rendering in renderings:
        assert QUERY_SECRET not in rendering
        assert FRAGMENT_SECRET not in rendering
        assert "sslmode=require" in rendering
        assert "***" in rendering


def test_a_url_with_no_password_is_left_alone() -> None:
    plain = "postgresql://elrond@db.internal:5432/quantbot"
    assert redact_database_url(plain) == plain


def test_a_sqlite_url_resolves_to_the_path_it_names() -> None:
    """The seam is exercised today rather than dormant until PostgreSQL exists."""
    relative = parse_database_url("sqlite:///var/lib/quantbot.db")
    assert relative.backend is DatabaseBackend.SQLITE
    assert relative.path == Path("var/lib/quantbot.db")

    absolute = parse_database_url("sqlite:////srv/quantbot.db")
    assert absolute.path == Path("/srv/quantbot.db")

    # A driver suffix names the same backend.
    assert parse_database_url("sqlite+pysqlite:///x.db").backend is DatabaseBackend.SQLITE

    # URL encoding names the filesystem path, not a literal percent sign.
    encoded = parse_database_url("sqlite:///stores/my%20database.db")
    assert encoded.path == Path("stores/my database.db")


def test_a_store_that_vanishes_with_the_process_is_not_a_durable_store() -> None:
    """Every authority rule here is about a store that outlives the process."""
    for rejected in (
        "sqlite://",
        "sqlite://host/x.db",
        "sqlite:///x.db?mode=ro",
        "sqlite:///x.db#client-secret",
    ):
        with pytest.raises(UnsupportedDatabaseUrl):
            parse_database_url(rejected)


def test_an_unrecognised_scheme_is_refused_rather_than_guessed_at() -> None:
    with pytest.raises(UnsupportedDatabaseUrl) as refused:
        parse_database_url("mysql://localhost/quantbot")
    assert refused.value.code == "DATABASE_URL_UNSUPPORTED"
    assert "postgresql" in str(refused.value) and "sqlite" in str(refused.value)

    with pytest.raises(UnsupportedDatabaseUrl):
        parse_database_url("   ")


def test_an_empty_direct_target_is_refused_instead_of_becoming_the_working_directory() -> None:
    for operation in (Database, Database.create, Database.migrate):
        with pytest.raises(DatabaseTargetError, match="target is empty"):
            operation("  ")  # type: ignore[operator]


def test_a_disabled_backend_cannot_open_create_or_migrate_a_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam must not become a way to acquire authority the path form never had.

    All three entry points refuse, and they refuse *before* touching the filesystem: a
    PostgreSQL URL must not leave a directory or a file named after itself behind, which is the
    quieter form of the same wrong signal `DatabaseNotFoundError` exists to give.
    """
    monkeypatch.chdir(tmp_path)
    targets = [
        DatabaseTarget(backend=DatabaseBackend.POSTGRESQL, location=POSTGRES_URL),
        POSTGRES_URL,
    ]

    for target in targets:
        for operation in (Database, Database.create, Database.migrate):
            with pytest.raises(BackendNotEnabled):
                operation(target)  # type: ignore[operator]

    assert list(tmp_path.iterdir()) == [], "a refused open left something on disk"


def test_cli_database_override_refuses_a_url_before_touching_the_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(BackendNotEnabled) as refused:
        main(
            ["create-database", "--database", POSTGRES_QUERY_URL, "--confirm"],
            output=StringIO(),
        )

    assert QUERY_SECRET not in str(refused.value)
    assert FRAGMENT_SECRET not in str(refused.value)
    assert list(tmp_path.iterdir()) == []


def test_a_url_shaped_legacy_path_setting_is_not_treated_as_a_filename() -> None:
    target = resolve_database_target({PATH_VARIABLE: POSTGRES_URL})

    assert target.backend is DatabaseBackend.POSTGRESQL
    with pytest.raises(BackendNotEnabled):
        target.require_enabled()


def test_the_facade_still_takes_a_plain_path_and_reports_its_target(tmp_path: Path) -> None:
    """`Database` stays the application-facing object; callers are not rewritten."""
    path = tmp_path / "quantbot.db"
    database = Database.create(path)
    try:
        assert database.path == path.resolve()
        assert database.target.backend is DatabaseBackend.SQLITE
        assert database.target.enabled is True
    finally:
        database.close()

    reopened = Database(database.target)
    try:
        assert reopened.path == path.resolve()
    finally:
        reopened.close()


def test_the_url_form_opens_the_same_store_the_path_form_does(tmp_path: Path) -> None:
    """Otherwise the seam is a second way to name a store that resolves somewhere else."""
    path = tmp_path / "quantbot.db"
    Database.create(path).close()

    from_url = resolve_database_target(
        {URL_VARIABLE: f"sqlite:///{path.as_posix()}"}
    )
    from_path = resolve_database_target({PATH_VARIABLE: str(path)})
    assert from_url.path.resolve() == from_path.path.resolve() == path.resolve()

    database = Database(from_url)
    try:
        assert database.path == path.resolve()
    finally:
        database.close()

    directly_from_url = Database(f"sqlite:///{path.as_posix()}")
    try:
        assert directly_from_url.path == path.resolve()
    finally:
        directly_from_url.close()


def test_the_runtime_refuses_to_start_against_a_backend_it_cannot_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused at `runtime_paths`, not at the first query.

    Earlier is better here: a disabled backend stops the process with its own message instead of
    surfacing later as a path that does not exist. There is deliberately no fallback -- quietly
    using SQLite when someone configured PostgreSQL is the empty-healthy-store failure with a
    different cause.
    """
    from quantbot.runtime import runtime_paths

    monkeypatch.delenv(PATH_VARIABLE, raising=False)
    monkeypatch.setenv(URL_VARIABLE, POSTGRES_URL)

    with pytest.raises(BackendNotEnabled) as refused:
        runtime_paths()
    assert SECRET not in str(refused.value)


def test_the_runtime_resolves_a_sqlite_url_to_the_same_store_as_the_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from quantbot.runtime import runtime_paths

    path = tmp_path / "quantbot.db"
    monkeypatch.delenv(PATH_VARIABLE, raising=False)
    monkeypatch.setenv(URL_VARIABLE, f"sqlite:///{path.as_posix()}")

    paths = runtime_paths()

    assert paths.target.backend is DatabaseBackend.SQLITE
    assert paths.database.resolve() == path.resolve(), (
        "the path form and the target must never disagree; resolving both from one call is why"
    )


def test_the_shadow_ledger_still_cannot_collide_with_a_url_configured_trading_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A safety boundary that the new configuration form could have quietly opened (#196).

    `_database_paths_are_isolated` refuses to write a shadow forecast ledger over the active
    trading store. It built its protected list from `QUANTBOT_DB_PATH` alone, so a
    `sqlite:///quantbot.db` URL -- which leaves that variable unset -- would have dropped the
    active store out of the list and turned the check green for exactly the collision it exists
    to refuse.
    """
    from quantbot.forecasting.cli import _database_paths_are_isolated

    active = tmp_path / "trading.db"
    source = tmp_path / "source.db"
    monkeypatch.delenv(PATH_VARIABLE, raising=False)
    monkeypatch.setenv(URL_VARIABLE, f"sqlite:///{active.as_posix()}")

    assert _database_paths_are_isolated(source, tmp_path / "shadow.db") is True
    assert _database_paths_are_isolated(source, active) is False, (
        "the URL-configured trading store fell out of the protected list"
    )

    # And a configuration this cannot resolve is refused rather than assumed isolated.
    monkeypatch.setenv(PATH_VARIABLE, str(active))
    assert _database_paths_are_isolated(source, tmp_path / "shadow.db") is False

    # A recognised but disabled backend is no more identifiable than an invalid target. Its URL
    # must not be taken as evidence that a file-backed shadow ledger is isolated from it.
    monkeypatch.delenv(PATH_VARIABLE)
    monkeypatch.setenv(URL_VARIABLE, POSTGRES_URL)
    assert _database_paths_are_isolated(source, tmp_path / "shadow.db") is False
