"""Why every research panel reads zero (#82, #48, #37).

Every research issue on this project reports the same symptom -- no experiment run, no hypothesis
registered, no candidate rejected -- and the console showed none of the reasons. An operator
reading empty research panels could reasonably conclude the system was working and had found
nothing.

That is the most expensive misreading available here, and the cause is mechanical: generating an
implementation from a registration needs a model, criticising it needs a *different* model, and
neither is configured. Three environment variables, not a research programme.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.console.state import read_console_state
from quantbot.research.composition import CRITIC, ENDPOINT, GENERATOR
from quantbot.storage import Database

NOW = datetime(2026, 8, 30, 20, 0, tzinfo=UTC)
CONTEXT = "QUANTBOT_MODEL_CONTEXT_TOKENS"


@pytest.fixture
def database(tmp_path: Path) -> Database:
    with Database.create(tmp_path / "research.db") as store:
        yield store


@pytest.fixture(autouse=True)
def clean_model_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from an unconfigured environment.

    Without this these pass or fail depending on whether the machine running them happens to have
    a model configured, which would make the suite's answer a property of the developer's shell.
    """
    for name in (ENDPOINT, GENERATOR, CRITIC, CONTEXT):
        monkeypatch.delenv(name, raising=False)


def panel(database: Database):  # type: ignore[no-untyped-def]
    with database.transaction() as session:
        return read_console_state(session, now=NOW).research_runtime


def attention(database: Database) -> tuple[str, ...]:
    with database.transaction() as session:
        return read_console_state(session, now=NOW).attention


def configure(monkeypatch: pytest.MonkeyPatch, *, generator: str, critic: str) -> None:
    monkeypatch.setenv(ENDPOINT, "http://localhost:11434/v1")
    monkeypatch.setenv(GENERATOR, generator)
    monkeypatch.setenv(CRITIC, critic)


def test_an_unconfigured_pipeline_names_the_missing_variables(database: Database) -> None:
    """The actionable part. "Research is not configured" sends somebody reading source."""
    result = panel(database)

    assert result is not None
    assert result.configured is False
    assert set(result.missing) == {ENDPOINT, GENERATOR, CRITIC}


def test_it_reaches_the_attention_list(database: Database) -> None:
    """A fact that explains every other empty panel belongs where an operator looks first."""
    items = attention(database)

    assert any("no model is configured" in item for item in items)
    assert any("no experiment can run" in item for item in items)


def test_a_configured_pipeline_raises_nothing(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A notice that fires on a working system is one an operator learns to scroll past."""
    configure(monkeypatch, generator="qwen2.5-coder:7b", critic="mistral:7b")

    result = panel(database)

    assert result is not None and result.configured is True
    assert result.missing == ()
    assert not [item for item in attention(database) if "no model is configured" in item]


def test_one_model_for_both_roles_is_reported_as_a_refusal(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case an operator would otherwise stare at with every variable set.

    A model asked to review its own proposal tends to find it sound, so the configuration is
    refused -- but nothing is *missing*, and reporting a missing-variable list would send someone
    hunting for a variable that is already there.
    """
    configure(monkeypatch, generator="mistral:7b", critic="mistral:7b")

    result = panel(database)

    assert result is not None
    assert result.configured is False
    assert result.missing == (), "nothing is unset; the configuration itself is the problem"
    assert result.refusal is not None
    assert "same model" in result.refusal

    assert any("refused" in item for item in attention(database))


def test_no_model_name_or_endpoint_is_ever_rendered(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Names only, never values -- the same rule the registry follows for the account id.

    A console is a surface. What it does not hold it cannot leak, and an endpoint URL can carry a
    host, a port and sometimes a token in a query string.
    """
    configure(monkeypatch, generator="secret-generator-name", critic="secret-critic-name")

    result = panel(database)
    body = TestClient(create_app(database)).get("/discovery").text

    assert result is not None and result.configured is True
    assert "secret-generator-name" not in body
    assert "secret-critic-name" not in body
    assert "11434" not in body


def test_the_discovery_page_explains_why_it_is_empty(database: Database) -> None:
    """Rendered, because the state-level assertion cannot see a template that forgot to use it."""
    body = TestClient(create_app(database)).get("/discovery").text

    assert "Research cannot run" in body
    assert "rather than because nothing was found" in body
    assert GENERATOR in body, "the missing variable is named on the page an operator lands on"
