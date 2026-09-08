"""Pinned, PAPER-only entry point for native Windows service tasks.

The scheduler supplies only deployment identity.  Application configuration comes from one
external dotenv file after inherited QuantBot and Alpaca values have been removed.  The module
then enters the ordinary :mod:`quantbot.cli` surface in this process, preserving all of its
runtime and kill-switch controls.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from quantbot import cli
from quantbot.config import Settings
from quantbot.domain.enums import BrokerEnvironment, TradingMode
from quantbot.storage import AlreadyLockedError, Database, SingleWriterLock
from quantbot.storage.target import AmbiguousDatabaseTarget, resolve_database_target

EXIT_REFUSED = 2
EXIT_CREDENTIALS_MISSING = 3
EXIT_DUPLICATE = 4
EXIT_COMPONENT_FAILED = 5

COMPONENT_COMMANDS: Mapping[str, list[str]] = {
    "console": ["console", "--host", "127.0.0.1", "--port", "8787"],
    "daemon": ["daemon"],
    "protection": ["protection-daemon"],
    "ingestion": ["sync-data"],
    "research": ["research-cycle", "--max-steps", "1", "--commit"],
    "report": ["report-weekly"],
}

BROKER_COMPONENTS = frozenset({"daemon", "protection", "ingestion", "report"})
BROKER_SECRETS = (
    "ALPACA_PAPER_API_KEY",
    "ALPACA_PAPER_API_SECRET",
    "ALPACA_LIVE_API_KEY",
    "ALPACA_LIVE_API_SECRET",
)
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_COMMIT = re.compile(r"[0-9a-fA-F]{40}\Z")


class ServiceRefusal(RuntimeError):
    """A stable refusal code whose rendering never includes configuration values."""


@dataclass(frozen=True, slots=True)
class ServiceConfiguration:
    component: str
    command: list[str]
    repository: Path
    state_directory: Path
    log_file: Path
    component_lock: Path | None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m quantbot.operations.service")
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--component", required=True, choices=tuple(COMPONENT_COMMANDS))
    parser.add_argument("--check", action="store_true")
    return parser


def repository_root() -> Path:
    """Return the checkout that supplied this module, independent of the caller's cwd."""
    return Path(__file__).resolve().parents[3]


def _application_name(name: str) -> bool:
    return name in Settings.model_fields or name.startswith(("QUANTBOT_", "ALPACA_"))


def _parse_dotenv(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as error:
        raise ServiceRefusal("ENV_FILE_UNREADABLE") from error
    result: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, raw_value = stripped.partition("=")
        name = name.strip()
        if not separator or not _ENVIRONMENT_NAME.fullmatch(name):
            raise ServiceRefusal("ENV_FILE_INVALID")
        if not _application_name(name):
            raise ServiceRefusal("ENV_FILE_VARIABLE_NOT_ALLOWED")
        if name in result:
            raise ServiceRefusal("ENV_FILE_DUPLICATE_VARIABLE")
        value = raw_value.strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise ServiceRefusal("ENV_FILE_INVALID")
            value = value[1:-1]
        if "\x00" in value:
            raise ServiceRefusal("ENV_FILE_INVALID")
        # Deliberately no expansion, escape decoding, shell parsing, or execution.
        result[name] = value
    return result


@contextlib.contextmanager
def isolated_application_environment(env_file: str | Path) -> Iterator[None]:
    path = Path(env_file)
    values = _parse_dotenv(path)
    previous = {name: value for name, value in os.environ.items() if _application_name(name)}
    for name in tuple(os.environ):
        if _application_name(name):
            os.environ.pop(name, None)
    os.environ.update(values)
    try:
        yield
    finally:
        for name in tuple(os.environ):
            if _application_name(name):
                os.environ.pop(name, None)
        os.environ.update(previous)


def _absolute_path(value: str | None) -> Path:
    if not value:
        raise ServiceRefusal("ABSOLUTE_RUNTIME_PATHS_REQUIRED")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ServiceRefusal("ABSOLUTE_RUNTIME_PATHS_REQUIRED")
    return path.resolve()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _git(repository: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ServiceRefusal("CHECKOUT_UNAVAILABLE") from error
    if completed.returncode != 0:
        raise ServiceRefusal("CHECKOUT_UNAVAILABLE")
    return completed.stdout.strip()


def _secret_present(value: object) -> bool:
    getter = getattr(value, "get_secret_value", None)
    return getter is not None and bool(getter().strip())


def validate_configuration(
    args: argparse.Namespace,
    *,
    repository: Path | None = None,
) -> ServiceConfiguration:
    env_file = _absolute_path(args.env_file)
    database_argument = _absolute_path(args.database)
    if not env_file.is_file():
        raise ServiceRefusal("ENV_FILE_UNREADABLE")
    if not _COMMIT.fullmatch(args.commit):
        raise ServiceRefusal("FULL_COMMIT_REQUIRED")

    try:
        settings = Settings()
    except Exception as error:  # Pydantic can include the rejected input in its message.
        raise ServiceRefusal("PAPER_CONFIGURATION_REQUIRED") from error
    if (
        settings.TRADING_MODE is not TradingMode.PAPER
        or settings.BROKER_ENVIRONMENT is not BrokerEnvironment.PAPER
        or settings.LIVE_TRADING_ACKNOWLEDGED
        or _secret_present(settings.ALPACA_LIVE_API_KEY)
        or _secret_present(settings.ALPACA_LIVE_API_SECRET)
    ):
        raise ServiceRefusal("PAPER_CONFIGURATION_REQUIRED")

    try:
        target = resolve_database_target()
    except AmbiguousDatabaseTarget as error:
        raise ServiceRefusal("DATABASE_TARGET_AMBIGUOUS") from error
    except Exception as error:
        raise ServiceRefusal("DATABASE_TARGET_INVALID") from error
    try:
        target.require_enabled()
    except Exception as error:
        raise ServiceRefusal("DATABASE_TARGET_INVALID") from error
    database = _absolute_path(str(target.path))
    lock = _absolute_path(os.environ.get("QUANTBOT_LOCK_PATH"))
    config = _absolute_path(os.environ.get("QUANTBOT_CONFIG"))
    reports = _absolute_path(os.environ.get("QUANTBOT_REPORTS_DIR"))
    if database != database_argument:
        raise ServiceRefusal("DATABASE_PIN_MISMATCH")
    if not config.is_file():
        raise ServiceRefusal("CONFIG_NOT_FOUND")
    if not database.is_file():
        raise ServiceRefusal("DATABASE_NOT_FOUND")
    if not lock.parent.is_dir() or not reports.is_dir():
        raise ServiceRefusal("RUNTIME_DIRECTORY_NOT_FOUND")

    active_repository = (repository or repository_root()).resolve()
    state_directory = database.parent
    logs = _absolute_path(
        os.environ.get("QUANTBOT_LOGS_DIR") or str(reports.parent / "logs")
    )
    if any(
        _inside(path, active_repository)
        for path in (state_directory, lock, reports, logs)
    ):
        raise ServiceRefusal("EXTERNAL_STATE_REQUIRED")

    head = _git(active_repository, "rev-parse", "--verify", "HEAD^{commit}").lower()
    if head != args.commit.lower():
        raise ServiceRefusal("CHECKOUT_PIN_MISMATCH")
    if _git(active_repository, "status", "--porcelain", "--untracked-files=all"):
        raise ServiceRefusal("CHECKOUT_NOT_CLEAN")

    try:
        opened = Database(database)
    except FileNotFoundError as error:
        raise ServiceRefusal("DATABASE_NOT_FOUND") from error
    except Exception as error:
        raise ServiceRefusal("DATABASE_INVALID") from error
    else:
        opened.close()

    paper_credentials = _secret_present(settings.ALPACA_PAPER_API_KEY) and _secret_present(
        settings.ALPACA_PAPER_API_SECRET
    )
    if args.component in BROKER_COMPONENTS and not paper_credentials:
        raise ServiceRefusal("PAPER_CREDENTIALS_REQUIRED")

    os.environ["QUANTBOT_GIT_COMMIT"] = args.commit.lower()
    component_lock = None
    if args.component not in {"daemon", "protection"}:
        component_lock = lock.with_suffix(f".{args.component}.lock")
    return ServiceConfiguration(
        component=args.component,
        command=list(COMPONENT_COMMANDS[args.component]),
        repository=active_repository,
        state_directory=state_directory,
        log_file=logs / f"{args.component}.log",
        component_lock=component_lock,
    )


def run_component(
    configuration: ServiceConfiguration,
    *,
    cli_runner: Callable[..., int] = cli.main,
) -> int:
    configuration.log_file.parent.mkdir(parents=True, exist_ok=True)
    original_cwd = Path.cwd()
    lock_context: contextlib.AbstractContextManager[object]
    lock_context = (
        contextlib.nullcontext()
        if configuration.component_lock is None
        else SingleWriterLock(configuration.component_lock)
    )
    try:
        with lock_context:
            os.chdir(configuration.state_directory)
            if configuration.component == "research":
                for name in BROKER_SECRETS:
                    os.environ.pop(name, None)
            with configuration.log_file.open("a", encoding="utf-8", buffering=1) as log:
                with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                    try:
                        return cli_runner(configuration.command, output=log)
                    except BaseException as error:  # scheduler receives a code, logs no values.
                        print(f"COMPONENT_FAILED {type(error).__name__}", file=log)
                        return EXIT_COMPONENT_FAILED
    except AlreadyLockedError:
        return EXIT_DUPLICATE
    except BaseException:
        print("SERVICE_COMPONENT_FAILED", file=sys.stderr)
        return EXIT_COMPONENT_FAILED
    finally:
        os.chdir(original_cwd)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with isolated_application_environment(args.env_file):
            configuration = validate_configuration(args)
            if args.check:
                print(f"validated {configuration.component}")
                return 0
            return run_component(configuration)
    except ServiceRefusal as error:
        print(f"quantbot service refused: {error}", file=sys.stderr)
        return (
            EXIT_CREDENTIALS_MISSING
            if str(error) == "PAPER_CREDENTIALS_REQUIRED"
            else EXIT_REFUSED
        )


if __name__ == "__main__":
    raise SystemExit(main())
