"""Every operator-relevant setting, what it currently is, and where it came from (#82 slice 1).

Configuration is scattered across four unrelated mechanisms: `Settings` reads the process
environment, `runtime_paths()` reads loose `QUANTBOT_*` variables, strategy behaviour lives in
YAML, and risk thresholds live in frozen Pydantic objects in code. Nothing could state the
effective value of all of it at once, which is the gap #82 opens with and the reason its first
slice is a registry rather than a page.

**The classification is derived, never declared.** #82's schema lists `safety_class` as a field,
and a hand-maintained label drifts on the first setting somebody adds in a hurry -- silently, and
in the permissive direction, because nobody re-audits a label. So:

* a field whose annotation is `SecretStr` is a `CREDENTIAL`, read off the type;
* a field of a model carrying a content-addressed `identity` is `EVIDENCE_BEARING`, read off the
  model rather than the field;
* the capital-boundary set is the one explicit list here, and a test checks it against the fields
  `validate_trading_safety` actually reads, so a new gate cannot be added without being covered.

**Nothing here is mutable at runtime, and saying so is the point.** #82 requires that a capability
which is intentionally not runtime-configurable still shows its effective value, its source, and
*why* it is immutable. That is this module's entire output today: `Settings` is environment-only
by construction, and the policy objects are frozen. A console built on this can render the whole
system honestly before it can change any of it, which is the correct order.

**Editing a versioned policy is not editing a value.** `PortfolioVetoPolicy.identity` is written
into `VetoRejection.rule_parameters_hash` and from there into the durable #57 record. Mutating a
threshold in place would make every historical refusal a claim about a rule that no longer
exists. #72 settled the rule -- a change creates a new version and prior evidence keeps the
authority it was measured under -- so `EVIDENCE_BEARING` settings report the identity they belong
to, and a future edit path mints a successor rather than writing a field.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Annotated, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, SecretStr, StringConstraints

from quantbot.config import Settings
from quantbot.risk.freshness import MarketFreshnessPolicy
from quantbot.risk.vetoes import PortfolioVetoPolicy

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


@runtime_checkable
class VersionedPolicy(Protocol):
    """Anything whose exact values are hashed into an identity that evidence cites."""

    @property
    def identity(self) -> str: ...


class SettingSource(StrEnum):
    """Where the effective value actually came from, as opposed to where it could come from."""

    #: An environment variable is set and its value is in force.
    ENVIRONMENT = "ENVIRONMENT"
    #: No environment variable is set; the code default is in force. Distinct from `ENVIRONMENT`
    #: because "nobody chose this" and "somebody chose exactly this" are different facts, and an
    #: operator reading a value needs to know which they are looking at.
    ENVIRONMENT_DEFAULT = "ENVIRONMENT_DEFAULT"
    #: A field of a frozen, content-addressed policy object.
    VERSIONED_POLICY = "VERSIONED_POLICY"


class SafetyClass(StrEnum):
    """What kind of harm changing a setting could do. Derived, not labelled."""

    #: Guards the real-capital boundary. Not settable from any surface, by any actor except the
    #: human operator editing their own environment.
    CAPITAL_BOUNDARY = "CAPITAL_BOUNDARY"
    #: A secret. Its presence is reportable; its value never is.
    CREDENTIAL = "CREDENTIAL"
    #: Participates in a content-addressed identity that evidence records cite. Changing it is
    #: minting a new version, never editing a number.
    EVIDENCE_BEARING = "EVIDENCE_BEARING"
    #: Everything else.
    OPERATIONAL = "OPERATIONAL"


#: Fields that decide whether real money can move. The one hand-maintained list in this module,
#: kept explicit because it is short and security-critical, and kept honest by
#: `test_the_capital_boundary_covers_every_field_the_safety_gate_reads`, which compares it
#: against the fields `validate_trading_safety` actually consults.
CAPITAL_BOUNDARY_FIELDS: frozenset[str] = frozenset(
    {
        # Mode and authorisation.
        "TRADING_MODE",
        "BROKER_ENVIRONMENT",
        "LIVE_TRADING_ACKNOWLEDGED",
        "KILL_SWITCH",
        "EXPECTED_ACCOUNT_ID",
        # Readiness gates. Every one of these prohibits new orders when false, so a surface
        # that presented them as ordinary operational flags would be offering a way to permit
        # trading by flipping a health bit. They were classified `OPERATIONAL` in the first
        # draft of this module and the test below caught it.
        "BROKER_HEALTHY",
        "MARKET_DATA_HEALTHY",
        "RISK_ENGINE_HEALTHY",
        "RECONCILIATION_SUCCESSFUL",
        # Reached through `_selected_credentials_complete`, which refuses any non-Alpaca
        # provider outright.
        "BROKER_PROVIDER",
    }
)

#: Fields whose value identifies the operator's account rather than configuring behaviour.
#: They are not secrets -- an account id authenticates nothing -- but this repository is public
#: and #82 routes this registry into a console and into exports, so they are reported as present
#: or absent and never by value. Whether an account is configured is the operationally useful
#: fact; which account it is is not.
IDENTIFYING_FIELDS: frozenset[str] = frozenset({"EXPECTED_ACCOUNT_ID"})

#: Stands in for a secret's value.
REDACTED = "[redacted]"
#: Stands in for an identifier that is present but must not be reproduced.
CONFIGURED = "[configured]"


class ConfigSetting(BaseModel):
    """One setting, with enough context to be acted on without reading the source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: Text
    subsystem: Text
    #: The value in force, rendered. `None` only where a value is genuinely unset.
    effective_value: str | None
    source: SettingSource
    safety_class: SafetyClass
    #: Always `False` today. Kept as a field rather than assumed, because #82's control plane
    #: will need somewhere to say which settings became changeable and this is where a reader
    #: will look for it.
    mutable_at_runtime: bool = False
    #: Why not, in words an operator can act on. Required whenever the answer is no.
    immutable_reason: Text
    #: For `EVIDENCE_BEARING` settings, the identity this value is hashed into.
    policy_identity: str | None = None

    @property
    def secret(self) -> bool:
        return self.safety_class is SafetyClass.CREDENTIAL


def _render(value: object) -> str:
    """Render a value for display, resolving nested policy to its identity.

    A nested frozen policy is not a scalar and printing its repr says nothing useful. Its
    `identity` is the honest summary: it is exactly what the surrounding policy hashes, so two
    registries differing in a nested rule differ visibly here.
    """
    if isinstance(value, BaseModel):
        identity = getattr(value, "identity", None)
        return str(identity) if identity is not None else value.model_dump_json()
    if isinstance(value, Mapping):
        return ", ".join(
            f"{_render(key)}={_render(item)}"
            for key, item in sorted(value.items(), key=lambda pair: _render(pair[0]))
        )
    if isinstance(value, frozenset | set | tuple | list):
        return ", ".join(sorted(_render(item) for item in value))
    return str(getattr(value, "value", value))


def _is_secret(model: type[BaseModel], field: str) -> bool:
    """Whether a field holds a secret, read off its annotation rather than its name.

    Naming conventions are the tempting shortcut here and they fail open: a field called
    `broker_token` that nobody thought to add to a list of patterns gets rendered.
    """
    annotation = model.model_fields[field].annotation
    return annotation is SecretStr or (
        annotation is not None and SecretStr in getattr(annotation, "__args__", ())
    )


def _settings_entries(settings: Settings, environ: Mapping[str, str]) -> Iterator[ConfigSetting]:
    for name in Settings.model_fields:
        secret = _is_secret(Settings, name)
        value = getattr(settings, name)
        if secret:
            rendered = REDACTED if value is not None else None
            safety = SafetyClass.CREDENTIAL
            reason = (
                "supplied through the environment and never written by the application; a "
                "surface that could set it would be a surface that could exfiltrate it"
            )
        elif name in CAPITAL_BOUNDARY_FIELDS:
            # Identifying fields are reported as configured-or-not even here. Which account the
            # operator holds is not a control, and this registry is read by surfaces that
            # publish.
            rendered = (
                None
                if value is None
                else CONFIGURED
                if name in IDENTIFYING_FIELDS
                else _render(value)
            )
            safety = SafetyClass.CAPITAL_BOUNDARY
            reason = (
                "decides whether real capital can move; only the human operator may change it, "
                "and only by editing their own environment"
            )
        else:
            rendered = (
                None
                if value is None
                else CONFIGURED
                if name in IDENTIFYING_FIELDS
                else _render(value)
            )
            safety = SafetyClass.OPERATIONAL
            reason = "read from the process environment at startup; changing it needs a restart"
        yield ConfigSetting(
            key=name,
            subsystem="broker" if name.startswith(("ALPACA", "BROKER", "EXPECTED")) else "safety",
            effective_value=rendered,
            source=(
                SettingSource.ENVIRONMENT
                if name in environ
                else SettingSource.ENVIRONMENT_DEFAULT
            ),
            safety_class=safety,
            immutable_reason=reason,
        )


def _policy_entries(subsystem: str, policy: VersionedPolicy) -> Iterator[ConfigSetting]:
    assert isinstance(policy, BaseModel)
    identity = policy.identity
    for name in type(policy).model_fields:
        rendered = _render(getattr(policy, name))
        yield ConfigSetting(
            key=name,
            subsystem=subsystem,
            effective_value=rendered,
            source=SettingSource.VERSIONED_POLICY,
            safety_class=SafetyClass.EVIDENCE_BEARING,
            immutable_reason=(
                f"hashed into {identity}, which durable refusals cite; changing it mints a new "
                "policy version rather than editing this one, so evidence measured under the "
                "old value keeps the authority it was measured under"
            ),
            policy_identity=identity,
        )


def _research_entries(environ: Mapping[str, str]) -> Iterator[ConfigSetting]:
    """The model configuration, which is read from the environment rather than from `Settings`.

    The registry's premise is that a setting cannot exist without appearing here, and these
    three did exist without appearing. `composition.model_configuration` reads them straight
    from `os.environ`, so walking `Settings.model_fields` never saw them -- and an operator
    looking at the configuration page had no way to learn that the entire research pipeline was
    unconfigured.

    **The endpoint is classified as a credential, and that is deliberate.** It is not a secret in
    the sense an API key is, but it is a URL that can embed one, and its value tells a reader
    nothing its presence does not already tell them. Rendering it would be the one place this
    console leaks a token, so it reports as configured-or-not like the account id does.

    **The model names are evidence-bearing.** `ExperimentManifest.models` records which model
    produced a generated artifact, and #45 exists because a model's training cutoff can
    contaminate a result. An experiment run under a different generator is a different
    experiment, so changing one of these is minting a new provenance rather than editing a
    setting -- exactly what that class means everywhere else here.
    """
    from quantbot.research.composition import API_KEY, CODER, CRITIC, ENDPOINT, GENERATOR

    credentials = (
        (
            ENDPOINT,
            "a model endpoint URL can carry a host, a port and sometimes a token; its presence "
            "is reportable and its value is not",
        ),
        (
            API_KEY,
            "a secret. Optional: a local endpoint legitimately has none, and an empty string is "
            "not a key",
        ),
    )
    for key, reason in credentials:
        present = bool(environ.get(key, "").strip())
        yield ConfigSetting(
            key=key,
            subsystem="research",
            effective_value="[configured]" if present else None,
            source=SettingSource.ENVIRONMENT,
            safety_class=SafetyClass.CREDENTIAL,
            immutable_reason=reason,
        )

    named = (
        (GENERATOR, "proposes hypotheses and writes implementations"),
        (CRITIC, "reviews them, and must differ from the generator"),
        (CODER, "writes implementations when set; defaults to the generator"),
    )
    for key, role in named:
        value = environ.get(key, "").strip()
        yield ConfigSetting(
            key=key,
            subsystem="research",
            effective_value=value or None,
            source=SettingSource.ENVIRONMENT,
            safety_class=SafetyClass.EVIDENCE_BEARING,
            immutable_reason=(
                f"{role}. Recorded into `ExperimentManifest.models`, so a result produced under "
                "a different model is a different experiment; changing this mints new provenance "
                "rather than editing a setting"
            ),
        )


def config_registry(
    *,
    settings: Settings | None = None,
    veto_policy: PortfolioVetoPolicy | None = None,
    freshness_policy: MarketFreshnessPolicy | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[ConfigSetting, ...]:
    """Every operator-relevant setting, read from the live objects rather than a list.

    Built by walking `model_fields` so a setting cannot exist without appearing here. A
    hand-maintained inventory would be accurate on the day it was written and quietly wrong
    afterwards, which is the failure mode #82's registry exists to replace.
    """
    import os

    active_environ = os.environ if environ is None else environ
    entries: list[ConfigSetting] = list(
        _settings_entries(settings or Settings(), active_environ)
    )
    entries.extend(_policy_entries("risk", veto_policy or PortfolioVetoPolicy()))
    entries.extend(_policy_entries("market-data", freshness_policy or MarketFreshnessPolicy()))
    entries.extend(_research_entries(active_environ))
    return tuple(sorted(entries, key=lambda item: (item.subsystem, item.key)))


__all__ = [
    "CAPITAL_BOUNDARY_FIELDS",
    "CONFIGURED",
    "IDENTIFYING_FIELDS",
    "REDACTED",
    "ConfigSetting",
    "SafetyClass",
    "SettingSource",
    "VersionedPolicy",
    "config_registry",
]
