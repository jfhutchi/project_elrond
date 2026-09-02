"""Where the durable store lives, expressed without assuming a filesystem (#196).

Today every caller says `Path`. That is true of SQLite and only of SQLite, and the assumption is
spread across runtime paths, CLI commands, the supervisor, the console and the deployment
scripts. #195 wants the option of PostgreSQL later; this issue creates the seam and enables
nothing, because a backend that has no host, no credentials and no proven schema parity is not a
backend anyone should be able to reach by setting an environment variable.

So a `postgresql://` URL is **recognised and refused**, with a stable code, rather than ignored or
half-supported. Recognising it is the point: a silent fallback to SQLite when someone points this
at a PostgreSQL instance would produce a pristine empty store that reports itself healthy, which
is the exact failure #86 exists to prevent, arriving through a new door.

**The authority model does not move.** A target says *where*; it grants nothing. Ordinary open
still does not create and does not migrate, `create` and `migrate` are still separate named
operations, and a schema mismatch still fails closed. Changing the shape of the location must not
become a way to acquire authority the path form never had.

**Credentials never reach a diagnostic.** A PostgreSQL URL carries a password, and this project
prints its configuration into CLI output, doctor reports, exceptions and the console. `__str__`,
`__repr__` and every message here go through the redactor, and a test asserts it against all of
them at once rather than trusting each call site to remember.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

#: The environment variable this project has always used. Unchanged, and still the default.
PATH_VARIABLE = "QUANTBOT_DB_PATH"

#: The explicit alternative. Accepts a SQLite URL today and recognises PostgreSQL in order to
#: refuse it, so the seam is exercised rather than dormant.
URL_VARIABLE = "QUANTBOT_DATABASE_URL"

DEFAULT_SQLITE_PATH = "quantbot.db"

REDACTED = "***"


class DatabaseBackend(StrEnum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


#: What this build can actually open. PostgreSQL joins this when #197 has proved semantic parity
#: on a real instance -- not when a connection string parses.
ENABLED_BACKENDS: frozenset[DatabaseBackend] = frozenset({DatabaseBackend.SQLITE})

#: URL schemes this project recognises, and the backend each names. A scheme absent here is
#: refused rather than guessed at: `mysql://` is not a typo for something supported.
_SCHEMES: Mapping[str, DatabaseBackend] = {
    "sqlite": DatabaseBackend.SQLITE,
    "postgresql": DatabaseBackend.POSTGRESQL,
    "postgres": DatabaseBackend.POSTGRESQL,
}


class DatabaseTargetError(RuntimeError):
    """A configured location that cannot be honoured. Carries a stable machine-readable code."""

    code = "DATABASE_TARGET_INVALID"


class AmbiguousDatabaseTarget(DatabaseTargetError):
    """Both a path and a URL were set.

    Refused rather than resolved by precedence. Inventing an order means one of the two settings
    silently does nothing, and the operator who set it believes it took effect -- which is how a
    system ends up writing to a store nobody meant to use.
    """

    code = "DATABASE_TARGET_AMBIGUOUS"


class UnsupportedDatabaseUrl(DatabaseTargetError):
    """The URL names a scheme this project does not recognise at all."""

    code = "DATABASE_URL_UNSUPPORTED"


class BackendNotEnabled(DatabaseTargetError):
    """The backend is recognised, understood, and deliberately not available in this build."""

    code = "BACKEND_NOT_ENABLED"


def redact_database_url(value: str) -> str:
    """Strip the password from a URL, leaving everything an operator needs to identify it.

    The username, host, port and database name stay: a redaction that removed them would make
    the diagnostic useless, and they are not the secret.
    """
    try:
        parts = urlsplit(value)
    except ValueError:
        return REDACTED
    if not parts.password:
        return value
    userinfo = f"{parts.username or ''}:{REDACTED}"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit(
        (parts.scheme, f"{userinfo}@{host}", parts.path, parts.query, parts.fragment)
    )


@dataclass(frozen=True, slots=True)
class DatabaseTarget:
    """Where the durable store is. Says nothing about permission to create or migrate it."""

    backend: DatabaseBackend
    #: A filesystem path for SQLite, a connection URL otherwise. Never rendered raw.
    location: str

    def __str__(self) -> str:
        return self.display

    def __repr__(self) -> str:
        # Overridden deliberately. The dataclass default prints `location` verbatim, and a
        # target reaches a log or a traceback through `repr` far more often than through `str`.
        return f"DatabaseTarget(backend={self.backend.value!r}, location={self.display!r})"

    @property
    def display(self) -> str:
        """Safe to print anywhere: CLI output, doctor, exceptions, the console."""
        if self.backend is DatabaseBackend.SQLITE:
            return self.location
        return redact_database_url(self.location)

    @property
    def enabled(self) -> bool:
        return self.backend in ENABLED_BACKENDS

    @property
    def path(self) -> Path:
        """The filesystem path, for SQLite only.

        Raises rather than returning something path-shaped for a URL. A caller that needs a file
        needs to know it does not have one, and a plausible-looking `Path("postgresql://...")`
        would be found much later and somewhere less helpful.
        """
        if self.backend is not DatabaseBackend.SQLITE:
            raise BackendNotEnabled(
                f"{self.backend.value} has no filesystem path; this caller assumes SQLite "
                f"({self.display})"
            )
        return Path(self.location).expanduser()

    def require_enabled(self) -> None:
        """Fail closed on a recognised-but-disabled backend, with the stable code."""
        if self.enabled:
            return
        raise BackendNotEnabled(
            f"{self.backend.value} is recognised but not enabled in this build "
            f"({self.display}). SQLite remains the only backend: PostgreSQL needs proven "
            "SQLite/PostgreSQL semantic parity (#197) and a real host before it can be opened, "
            "and a connection string that parses is not either of those"
        )


def parse_database_url(value: str) -> DatabaseTarget:
    """Turn a URL into a target. Recognises PostgreSQL in order to refuse it later."""
    text = value.strip()
    if not text:
        raise UnsupportedDatabaseUrl("database URL is empty")
    parts = urlsplit(text)
    scheme = parts.scheme.split("+", maxsplit=1)[0].lower()
    backend = _SCHEMES.get(scheme)
    if backend is None:
        raise UnsupportedDatabaseUrl(
            f"unrecognised database URL scheme {scheme or '(none)'!r}; this project understands "
            + ", ".join(sorted(_SCHEMES))
        )
    if backend is DatabaseBackend.SQLITE:
        # `sqlite:///relative.db` and `sqlite:////absolute.db`, the SQLAlchemy convention.
        # In-memory is rejected: every authority rule here is about a durable store, and a
        # store that vanishes with the process cannot be the one holding the ledger.
        # `sqlite:///relative.db` gives path `/relative.db`; `sqlite:////abs/x.db` gives
        # `//abs/x.db`. Dropping one leading slash yields the intended path in both cases.
        location = parts.path[1:] if parts.path.startswith("/") else parts.path
        if parts.netloc or not location:
            raise UnsupportedDatabaseUrl(
                "a SQLite URL is sqlite:///<path>; a host or an empty path is not a durable store"
            )
        return DatabaseTarget(backend=DatabaseBackend.SQLITE, location=location)
    return DatabaseTarget(backend=backend, location=text)


def resolve_database_target(
    environ: Mapping[str, str] | None = None,
    *,
    default_path: str = DEFAULT_SQLITE_PATH,
) -> DatabaseTarget:
    """Read the configured target. Unset means exactly what it always meant.

    Three rules, and the third is the one worth stating:

    1. `QUANTBOT_DB_PATH` alone -> SQLite at that path, unchanged from every prior release.
    2. `QUANTBOT_DATABASE_URL` alone -> whatever the URL names, refused later if not enabled.
    3. **Both -> a hard error.** No precedence is invented, because a precedence rule means one
       of the two settings silently does nothing while the operator who set it believes it took
       effect.
    """
    source = os.environ if environ is None else environ
    path_value = (source.get(PATH_VARIABLE) or "").strip()
    url_value = (source.get(URL_VARIABLE) or "").strip()

    if path_value and url_value:
        raise AmbiguousDatabaseTarget(
            f"{PATH_VARIABLE} and {URL_VARIABLE} are both set "
            f"({path_value!r} and {redact_database_url(url_value)!r}). Set one: choosing between "
            "them here would make the other silently do nothing"
        )
    if url_value:
        return parse_database_url(url_value)
    return DatabaseTarget(backend=DatabaseBackend.SQLITE, location=path_value or default_path)


def sqlite_target(path: str | Path) -> DatabaseTarget:
    """The target a plain path has always meant."""
    return DatabaseTarget(backend=DatabaseBackend.SQLITE, location=str(path))


def coerce_target(value: str | Path | DatabaseTarget) -> DatabaseTarget:
    """Accept either form, so the seam does not force a rewrite of every call site."""
    return value if isinstance(value, DatabaseTarget) else sqlite_target(value)


__all__ = [
    "DEFAULT_SQLITE_PATH",
    "ENABLED_BACKENDS",
    "PATH_VARIABLE",
    "REDACTED",
    "URL_VARIABLE",
    "AmbiguousDatabaseTarget",
    "BackendNotEnabled",
    "DatabaseBackend",
    "DatabaseTarget",
    "DatabaseTargetError",
    "UnsupportedDatabaseUrl",
    "coerce_target",
    "parse_database_url",
    "redact_database_url",
    "resolve_database_target",
    "sqlite_target",
]
