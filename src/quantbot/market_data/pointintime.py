"""When a value became knowable, enforced rather than remembered (#17).

Every result in `REFUTED.md` rests on the assumption that a value was knowable when the
backtest used it, and until now that assumption was upheld by hand every time. Two of the three
analysis errors in cycles 11-12 were timing errors of exactly this family. Making it structural
is worth more than any agent in the roadmap, because discipline that depends on remembering is
discipline that eventually fails quietly.

The model is two timestamps, never one:

* `observed_at` -- the moment the value describes.
* `available_at` -- the moment it could first be known.

For a daily bar those differ by the session close plus publication lag. For a macro series they
differ by weeks, and the series is then *revised*, so the value known on a date is not the value
in today's file. That is why a vintage is part of the identity of a datum rather than a
footnote: a conclusion whose inputs changed underneath it cannot be reproduced, and worse,
cannot be shown to have changed.

A feature adds a third lag. A 200-day moving average computed at a close is not usable until
the next open, and a feature over an unavailable input is unavailable however recent its own
timestamp is. `FeatureSpec.available_at` composes those so the answer is computed once rather
than reasoned about per experiment.

Capabilities are declared, and asking a provider for something it cannot serve raises rather
than returning empty. Silence is how a survivorship-free study quietly becomes a live-only one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Generic, Protocol, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from quantbot.market_data.base import MarketDataError
from quantbot.market_data.instruments import Instrument

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Value = TypeVar("Value")


class PointInTimeViolation(MarketDataError):
    """Raised when a value is requested before the moment it could have been known."""

    def __init__(self, label: str, available_at: datetime, as_of: datetime) -> None:
        self.label = label
        self.available_at = available_at
        self.as_of = as_of
        super().__init__(
            f"{label} was not available until {available_at.isoformat()}, "
            f"but was requested as of {as_of.isoformat()}"
        )


class UnsupportedCapability(MarketDataError):
    """Raised when a provider is asked for data it does not serve.

    Deliberately an error rather than an empty result: a cross-sectional study that silently
    receives no delisted instruments produces a survivorship-biased answer and no warning.
    """

    def __init__(self, provider: str, capability: Capability) -> None:
        self.provider = provider
        self.capability = capability
        super().__init__(f"{provider} does not serve {capability.value}")


class Capability(StrEnum):
    BARS = "BARS"
    QUOTES = "QUOTES"
    CORPORATE_ACTIONS = "CORPORATE_ACTIONS"
    #: Delisted instruments and their termination dates. Without this, cross-sectional results
    #: are survivorship-biased and there is no way to tell from the data.
    DELISTINGS = "DELISTINGS"
    FUNDAMENTALS = "FUNDAMENTALS"
    MACRO_SERIES = "MACRO_SERIES"
    OPTIONS_DERIVED = "OPTIONS_DERIVED"
    FX = "FX"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Vintage(FrozenModel):
    """The identity of a dataset *as it stood*, which is not the same as the dataset."""

    provider: Text
    dataset: Text
    #: Vendor's own version marker where one exists, otherwise the retrieval date.
    vintage: Text
    retrieved_at: datetime
    #: Set when this vintage supersedes an earlier one, so a revision is visible as a revision.
    supersedes: str | None = None


class Availability(FrozenModel):
    """What a datum describes, and when it could first be known."""

    observed_at: datetime
    available_at: datetime

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.available_at < self.observed_at:
            raise ValueError("a value cannot be available before the moment it describes")
        return self

    def known_by(self, as_of: datetime) -> bool:
        return self.available_at <= as_of


class Observation(FrozenModel, Generic[Value]):
    """One value with its availability and vintage attached, so lineage travels with it."""

    label: Text
    availability: Availability
    vintage: Vintage
    value: Value

    def require_known_by(self, as_of: datetime) -> Self:
        if not self.availability.known_by(as_of):
            raise PointInTimeViolation(self.label, self.availability.available_at, as_of)
        return self


def knowable(
    observations: Iterable[Observation[Value]], as_of: datetime
) -> tuple[Observation[Value], ...]:
    """Filter to what was actually knowable. The complement is look-ahead, by definition."""
    return tuple(item for item in observations if item.availability.known_by(as_of))


def latest_knowable(
    observations: Iterable[Observation[Value]], as_of: datetime
) -> Observation[Value] | None:
    """The most recent value knowable at `as_of`, which is what a decision may actually use."""
    candidates = knowable(observations, as_of)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.availability.observed_at)


class FeatureSpec(FrozenModel):
    """A transformation, its lookback, and when its output becomes usable.

    The transformation version is part of the identity. A feature recomputed with changed code
    is a different feature, and a manifest that records only the name cannot tell them apart.
    """

    name: Text
    #: Observations required before the first value exists.
    lookback_observations: int = Field(ge=1)
    #: Lag between the last input being available and this feature being usable. A close-based
    #: signal acted on at the next open is one session.
    publication_lag: timedelta = timedelta(0)
    transformation_version: Text
    #: Inputs, by label, so lineage is traceable through a chain of features.
    inputs: tuple[Text, ...] = Field(min_length=1)

    def available_at(self, inputs: Sequence[Availability]) -> datetime:
        """When this feature may first be used, given when its inputs became knowable.

        A feature is never available before its slowest input. This is where look-ahead most
        often enters: the feature's own timestamp looks recent while an input behind it was not
        published until later.
        """
        if len(inputs) < self.lookback_observations:
            raise ValueError(
                f"{self.name} needs {self.lookback_observations} observations, "
                f"{len(inputs)} supplied"
            )
        window = sorted(inputs, key=lambda item: item.observed_at)[-self.lookback_observations :]
        return max(item.available_at for item in window) + self.publication_lag

    def materialize(
        self,
        value: Value,
        *,
        inputs: Sequence[Availability],
        vintage: Vintage,
    ) -> Observation[Value]:
        """Attach availability and lineage to a computed feature value.

        Takes the inputs' availabilities rather than the inputs: the values themselves are the
        caller's business, and the only thing that decides when this result may be used is when
        the things behind it became knowable.
        """
        window = sorted(inputs, key=lambda item: item.observed_at)
        return Observation(
            label=f"{self.name}@{self.transformation_version}",
            availability=Availability(
                observed_at=window[-1].observed_at,
                available_at=self.available_at(inputs),
            ),
            vintage=vintage,
            value=value,
        )


class ResearchDataProvider(Protocol):
    """What research asks for data through, so a hypothesis never imports a vendor SDK."""

    @property
    def name(self) -> str: ...

    def capabilities(self) -> frozenset[Capability]:
        """What this provider actually serves. Anything absent raises rather than returns []."""
        ...

    def vintage(self, dataset: str, *, retrieved_at: datetime) -> Vintage:
        """The clock is passed in, not read.

        A vintage is defined by when it was pulled, and every other component here takes `now`
        as an argument rather than calling `datetime.now()`. A provider that read the clock
        itself would make its own output untestable and non-reproducible.
        """
        ...

    def instruments(self, *, as_of: datetime) -> tuple[Instrument, ...]:
        """Point-in-time membership, including instruments delisted before `as_of`."""
        ...


def require(provider: ResearchDataProvider, capability: Capability) -> None:
    """Fail loudly when a study asks for something the provider cannot give it."""
    if capability not in provider.capabilities():
        raise UnsupportedCapability(provider.name, capability)


def session_availability(
    close: datetime, *, publication_lag: timedelta = timedelta(0)
) -> Availability:
    """A daily bar describes its close and is knowable only after it, plus any vendor lag."""
    if close.tzinfo is None or close.utcoffset() is None:
        raise ValueError("session close must be timezone-aware")
    normalized = close.astimezone(UTC)
    return Availability(
        observed_at=normalized, available_at=normalized + publication_lag
    )


__all__ = [
    "Availability",
    "Capability",
    "FeatureSpec",
    "Observation",
    "PointInTimeViolation",
    "ResearchDataProvider",
    "UnsupportedCapability",
    "Vintage",
    "knowable",
    "latest_knowable",
    "require",
    "session_availability",
]
