"""The configuration registry, and the ways an inventory of settings goes quietly wrong (#82).

A registry is only worth having if it cannot fall behind the thing it describes. Every test here
compares the registry against the live models rather than against a fixture, because a fixture
listing the settings would be a second hand-maintained inventory drifting alongside the first.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import SecretStr

from quantbot.config import Settings, validate_trading_safety
from quantbot.operations.registry import (
    CAPITAL_BOUNDARY_FIELDS,
    CONFIGURED,
    IDENTIFYING_FIELDS,
    REDACTED,
    SafetyClass,
    SettingSource,
    config_registry,
)
from quantbot.risk.freshness import MarketFreshnessPolicy
from quantbot.risk.vetoes import PortfolioVetoPolicy

ACCOUNT = "acct-0123456789-secret-looking"


def registry(**overrides: object):
    fields: dict[str, object] = {
        "settings": Settings(
            EXPECTED_ACCOUNT_ID=ACCOUNT,
            ALPACA_PAPER_API_KEY=SecretStr("pk-do-not-print"),
            ALPACA_PAPER_API_SECRET=SecretStr("sk-do-not-print"),
        ),
        "environ": {},
    }
    fields.update(overrides)
    return config_registry(**fields)  # type: ignore[arg-type]


def test_the_registry_is_derived_from_the_models_not_a_parallel_list() -> None:
    """The property that keeps it true: a setting cannot exist without appearing here.

    Compared against `model_fields` at runtime rather than against a literal, so replacing the
    derivation with a hand-maintained list passes today and fails the moment a field is added --
    which is the only failure mode that matters for an inventory.
    """
    rows = registry()
    by_subsystem: dict[str, set[str]] = {}
    for row in rows:
        by_subsystem.setdefault(row.subsystem, set()).add(row.key)

    assert by_subsystem["risk"] == set(PortfolioVetoPolicy.model_fields)
    assert by_subsystem["market-data"] == set(MarketFreshnessPolicy.model_fields)
    assert (by_subsystem["broker"] | by_subsystem["safety"]) == set(Settings.model_fields)


def test_no_secret_value_reaches_the_registry() -> None:
    """Presence is reportable; the value never is.

    Asserted against the actual secret strings rather than against the redaction marker, because
    a test that only checked for `[redacted]` would pass a row that printed both.
    """
    rows = registry()
    rendered = " ".join(row.effective_value or "" for row in rows)

    assert "pk-do-not-print" not in rendered
    assert "sk-do-not-print" not in rendered

    secrets = [row for row in rows if row.safety_class is SafetyClass.CREDENTIAL]
    assert secrets, "the fixture must configure credentials or this proves nothing"
    for row in secrets:
        assert row.effective_value in (REDACTED, None)
        assert row.secret is True


def test_a_secret_is_recognised_by_its_type_not_its_name() -> None:
    """Naming conventions fail open: the field nobody added to the pattern list gets printed."""
    rows = {row.key: row for row in registry()}

    for name, field in Settings.model_fields.items():
        annotation = field.annotation
        holds_secret = annotation is SecretStr or SecretStr in getattr(
            annotation, "__args__", ()
        )
        if holds_secret:
            assert rows[name].safety_class is SafetyClass.CREDENTIAL, name


def test_an_account_identifier_is_reported_as_configured_and_never_reproduced() -> None:
    """Not a secret -- an account id authenticates nothing -- but this repository is public.

    #82 routes this registry into a console and into exports. Whether an account is configured
    is the operationally useful fact; which account it is is not, and the difference stops
    mattering once something publishes it.
    """
    rows = {row.key: row for row in registry()}
    rendered = " ".join(row.effective_value or "" for row in registry())

    assert ACCOUNT not in rendered
    for name in IDENTIFYING_FIELDS:
        assert rows[name].effective_value == CONFIGURED

    # Explicitly `None` rather than an unconfigured `Settings()`, which would read the real
    # environment and pick up whatever account this machine has configured.
    absent = {row.key: row for row in registry(settings=Settings(EXPECTED_ACCOUNT_ID=None))}
    assert absent["EXPECTED_ACCOUNT_ID"].effective_value is None, (
        "an unset identifier must read as unset, not as configured"
    )


def test_the_capital_boundary_covers_every_field_the_safety_gate_reads() -> None:
    """The one hand-maintained list here, checked against the code it is supposed to mirror.

    `validate_trading_safety` is what actually decides whether real money may move. If it grows
    a new input and the registry does not classify it, a console would render that field as an
    ordinary operational setting -- which is precisely the mistake this classification exists to
    prevent.
    """
    # The gate reaches some settings through module-private helpers -- `BROKER_PROVIDER` only
    # via `_selected_credentials_complete` -- so scanning the entry point alone would detect it
    # by accident and miss the next one entirely.
    import quantbot.config as config_module

    sources = [inspect.getsource(validate_trading_safety)]
    sources += [
        inspect.getsource(member)
        for name, member in vars(config_module).items()
        if name.startswith("_") and inspect.isfunction(member)
    ]
    source = chr(10).join(sources)
    consulted = {name for name in Settings.model_fields if name in source}

    assert consulted, "the gate must read some settings, or this test is measuring nothing"

    # Asserted against the registry's output rather than against `CAPITAL_BOUNDARY_FIELDS`,
    # because the credential fields are also gate inputs and carry the *stricter* class -- their
    # values never render at all. The property that matters is that no input to the gate can be
    # presented as an ordinary operational setting.
    rows = {row.key: row for row in registry()}
    protected = {SafetyClass.CAPITAL_BOUNDARY, SafetyClass.CREDENTIAL}
    mislabelled = sorted(name for name in consulted if rows[name].safety_class not in protected)
    assert not mislabelled, (
        f"{mislabelled} decide whether capital can move but are classified as ordinary "
        "settings, so a surface would present them as routine configuration"
    )
    assert CAPITAL_BOUNDARY_FIELDS <= consulted, (
        f"{sorted(CAPITAL_BOUNDARY_FIELDS - consulted)} are declared as the capital boundary "
        "but the safety gate does not read them; a stale entry here is a claim about a control "
        "that no longer exists"
    )


def test_nothing_is_runtime_mutable_and_every_entry_says_why() -> None:
    """#82 requires an immutable capability to still show its value, source and reason.

    Today that is the whole answer: `Settings` is environment-only by construction and the
    policy objects are frozen. A console can therefore render the system honestly before it can
    change any of it, which is the right order to build in.
    """
    rows = registry()

    assert rows, "an empty registry would satisfy every other assertion here"
    for row in rows:
        assert row.mutable_at_runtime is False
        assert row.immutable_reason.strip(), row.key


def test_a_policy_field_names_the_identity_it_is_hashed_into() -> None:
    """Changing one is minting a version, not editing a number, and the row has to say so."""
    policy = PortfolioVetoPolicy()
    rows = [row for row in registry(veto_policy=policy) if row.subsystem == "risk"]

    assert rows
    for row in rows:
        assert row.safety_class is SafetyClass.EVIDENCE_BEARING
        assert row.source is SettingSource.VERSIONED_POLICY
        assert row.policy_identity == policy.identity
        assert "mints a new" in row.immutable_reason

    # And the identity actually moves with the value, so two registries are distinguishable.
    tighter = PortfolioVetoPolicy(correlation_threshold=policy.correlation_threshold / 2)
    other = [row for row in registry(veto_policy=tighter) if row.subsystem == "risk"]
    assert other[0].policy_identity != rows[0].policy_identity


def test_a_chosen_value_is_distinguished_from_an_unchosen_one() -> None:
    """"Nobody set this" and "somebody set exactly this" are different facts about a value."""
    unset = {row.key: row for row in registry(environ={})}
    chosen = {row.key: row for row in registry(environ={"TRADING_MODE": "PAPER"})}

    assert unset["TRADING_MODE"].source is SettingSource.ENVIRONMENT_DEFAULT
    assert chosen["TRADING_MODE"].source is SettingSource.ENVIRONMENT
    assert unset["TRADING_MODE"].effective_value == chosen["TRADING_MODE"].effective_value, (
        "the distinction is provenance, not value"
    )


def test_a_nested_policy_renders_as_its_identity_rather_than_a_repr() -> None:
    """A nested frozen rule is not a scalar, and its repr says nothing a reader can use."""
    rows = {row.key: row for row in registry() if row.subsystem == "risk"}

    assert rows["freshness"].effective_value == PortfolioVetoPolicy().freshness.identity
    assert rows["freshness"].effective_value is not None
    assert rows["freshness"].effective_value.startswith("freshness-policy-v")


def test_the_cli_view_leaks_neither_credentials_nor_the_account_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The path that actually publishes, asserted through the command rather than the library.

    `quantbot config` is meant to be pasteable into an issue on a public repository. The
    registry redacting correctly is necessary; the command inheriting that is the property that
    matters, and it is a different line of code.
    """
    import json
    from io import StringIO

    from quantbot.cli import main

    monkeypatch.setenv("EXPECTED_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "pk-do-not-print")
    monkeypatch.setenv("ALPACA_PAPER_API_SECRET", "sk-do-not-print")

    output = StringIO()
    assert main(["config"], output=output) == 0
    rendered = output.getvalue()

    assert ACCOUNT not in rendered
    assert "pk-do-not-print" not in rendered
    assert "sk-do-not-print" not in rendered

    payload = json.loads(rendered)
    assert payload["ok"] is True
    assert payload["settings"], "an empty report would satisfy every assertion above"
    assert payload["mutable_at_runtime"] == 0
    keys = {row["key"] for row in payload["settings"]}
    assert {"EXPECTED_ACCOUNT_ID", "correlation_threshold", "KILL_SWITCH"} <= keys
