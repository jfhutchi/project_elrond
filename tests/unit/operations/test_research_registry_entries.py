"""The model configuration belongs in the registry like everything else (#82).

The registry's premise is that a setting cannot exist without appearing here -- it walks
`model_fields` rather than a hand-maintained list, precisely so an inventory cannot go quietly
stale. The three model variables existed without appearing, because
`composition.model_configuration` reads them straight from `os.environ`.

So an operator on the configuration page had no way to learn that the entire research pipeline
was unconfigured, which is the same gap #155 closed on the discovery page from the other side.
"""

from __future__ import annotations

import pytest

from quantbot.operations.registry import SafetyClass, SettingSource, config_registry
from quantbot.research.composition import API_KEY, CODER, CRITIC, ENDPOINT, GENERATOR


def registry(**environ: str):  # type: ignore[no-untyped-def]
    """The registry over an explicit environment, so a developer's shell cannot change it."""
    return {item.key: item for item in config_registry(environ=environ)}


def test_every_model_variable_appears(  # type: ignore[no-untyped-def]
) -> None:
    """A setting that exists and is not listed is the failure this registry exists to prevent."""
    entries = registry()

    for key in (ENDPOINT, GENERATOR, CRITIC, CODER, API_KEY):
        assert key in entries, f"{key} is read at runtime and absent from the registry"
        assert entries[key].subsystem == "research"


def test_the_endpoint_is_never_rendered(  # type: ignore[no-untyped-def]
) -> None:
    """Classified as a credential deliberately, and this is the assertion that keeps it so.

    It is not a secret the way an API key is, but it is a URL that can embed one, and its value
    tells a reader nothing its presence does not. Rendering it would be the one place this
    console leaks a token.
    """
    entries = registry(**{ENDPOINT: "http://user:token@internal-host:11434/v1"})

    endpoint = entries[ENDPOINT]
    assert endpoint.safety_class is SafetyClass.CREDENTIAL
    assert endpoint.secret is True
    assert endpoint.effective_value == "[configured]"
    assert "token" not in str(endpoint.effective_value)
    assert "internal-host" not in str(endpoint.effective_value)


def test_an_unset_endpoint_is_absent_rather_than_configured(  # type: ignore[no-untyped-def]
) -> None:
    """`None` and `[configured]` are the two answers; there is no third."""
    assert registry()[ENDPOINT].effective_value is None
    assert registry(**{ENDPOINT: "   "})[ENDPOINT].effective_value is None, (
        "whitespace is not a configured endpoint"
    )


def test_model_names_are_evidence_bearing(  # type: ignore[no-untyped-def]
) -> None:
    """`ExperimentManifest.models` records which model produced a generated artifact.

    An experiment run under a different generator is a different experiment -- #45 exists
    because a model's training cutoff can contaminate a result -- so changing one of these is
    minting new provenance rather than editing a setting.
    """
    entries = registry(**{GENERATOR: "qwen2.5-coder:7b", CRITIC: "mistral:7b"})

    assert entries[GENERATOR].safety_class is SafetyClass.EVIDENCE_BEARING
    assert entries[CRITIC].safety_class is SafetyClass.EVIDENCE_BEARING
    assert entries[GENERATOR].effective_value == "qwen2.5-coder:7b"
    assert "different experiment" in entries[GENERATOR].immutable_reason


def test_model_names_are_shown_because_they_are_not_secrets(  # type: ignore[no-untyped-def]
) -> None:
    """The endpoint is hidden and the model is not, and the distinction is the point.

    Knowing which model ran is what makes a result auditable. Hiding it would remove the one
    piece of provenance a reader most needs, to protect something that is not a secret.
    """
    entries = registry(**{GENERATOR: "qwen2.5-coder:7b"})

    assert entries[GENERATOR].secret is False
    assert entries[GENERATOR].effective_value == "qwen2.5-coder:7b"


def test_the_source_says_it_came_from_the_environment(  # type: ignore[no-untyped-def]
) -> None:
    """These are not `Settings` fields and must not claim to be."""
    entries = registry(**{GENERATOR: "mistral:7b"})

    assert entries[GENERATOR].source is SettingSource.ENVIRONMENT
    assert entries[ENDPOINT].source is SettingSource.ENVIRONMENT


@pytest.mark.parametrize("key", [ENDPOINT, GENERATOR, CRITIC, CODER, API_KEY])
def test_every_entry_says_why_it_cannot_be_changed_here(key: str) -> None:  # type: ignore[no-untyped-def]
    """An immutable setting with no stated reason sends an operator to read source."""
    entry = registry()[key]

    assert entry.mutable_at_runtime is False
    assert len(entry.immutable_reason) > 20, key
