"""What a Kronos feature costs the multiple-testing budget (#36, #23, #11).

The shadow collector records forecasts with unusually complete provenance and records **no search
cardinality**. That is not a defect while forecasts sit in the shadow plane, because nothing
consumes them and a forecast is not evidence. It becomes one the moment a forecast turns into a
feature in a registered hypothesis -- which is exactly what #36's first scientific question
requires ("does zero-shot Kronos add incremental predictive information").

A forecaster is the canonical search-laundering machine. Twenty horizons by ten lookbacks by five
sampling configurations is a thousand data-dependent configurations, and if the best-looking one
becomes `kronos_expected_return_5d` in a hypothesis, the project has performed a thousand trials
and registered one. Every later hypothesis is then judged against a luck bar that is too low, and
nothing in the ledger says why.

**The good news is that nobody has to be trusted here.** The burden is a countable fact:
`model_forecasts` records the lookback, the horizon and the full request for every forecast ever
produced. So this module counts what was actually run rather than accepting a disclosure --
`SearchOrigin.MEASURED` in the sense #23 means it, derived from a row rather than asserted.

**The unit is configurations tried, not forecasts produced**, and the distinction decides whether
Kronos is usable at all. A collector running one configuration daily for a year produces 250
forecasts and has tested *one* idea about that configuration; charging 250 trials would make the
shadow plane unusable and would be wrong. Running 200 configurations over the same window has
tested 200 ideas. So identity is the configuration -- lookback, horizon, and everything in
`KronosConfig` that changes what the model computes -- and repeated application of one
configuration across dates is a time series rather than a search.

Failed and skipped attempts count. A configuration that was tried and produced nothing still
looked at the data, and excluding it would let a search be shrunk by discarding its
disappointments -- which is the original defect wearing different clothes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.forecasting.schema import model_forecasts

#: Fields of `KronosConfig` that change what the model computes, and therefore make a run a
#: different experiment rather than a repetition of the same one. Deliberately excludes pure
#: provenance -- `integration_version` and the weight hashes identify the artifact, and bumping a
#: package version is not a new idea about the market.
#:
#: `seed` is included. Re-running one configuration under a different seed and keeping the better
#: result is search, and it is the cheapest kind to perform by accident.
SEARCH_DIMENSIONS: tuple[str, ...] = (
    "model_id",
    "model_revision",
    "tokenizer_revision",
    "temperature",
    "top_p",
    "top_k",
    "sample_count",
    "seed",
    "max_context",
    "input_transform_version",
)


@dataclass(frozen=True, slots=True)
class ForecastBurden:
    """How much data-dependent search a set of Kronos forecasts represents."""

    configurations: int
    forecasts: int
    symbols: tuple[str, ...]
    start: date | None
    end: date | None

    @property
    def note(self) -> str:
        """The sentence that belongs on the `search_runs` row."""
        return (
            f"{self.configurations} distinct Kronos configurations over {self.forecasts} "
            f"forecasts on {', '.join(self.symbols) or 'no symbols'}"
        )


def forecast_burden(
    session: Session,
    *,
    symbols: Sequence[str] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> ForecastBurden:
    """Count the distinct Kronos configurations actually run over a window.

    Counted from the forecast ledger rather than reported by a caller, so a registration citing
    this is `MEASURED` in the sense #23 requires: derived from rows, not asserted by the party
    that benefits from a smaller number.
    """
    statement = select(
        model_forecasts.c.symbol,
        model_forecasts.c.as_of,
        model_forecasts.c.lookback,
        model_forecasts.c.horizon,
        model_forecasts.c.request_json,
    )
    if symbols:
        statement = statement.where(model_forecasts.c.symbol.in_(list(symbols)))
    if start is not None:
        statement = statement.where(model_forecasts.c.as_of >= start.isoformat())
    if end is not None:
        # Inclusive of the end date: `as_of` carries a time, so a plain `<=` on the date string
        # would drop everything after midnight on the last day.
        statement = statement.where(model_forecasts.c.as_of < _day_after(end))

    rows = session.execute(statement).all()
    seen: set[tuple[object, ...]] = set()
    found_symbols: set[str] = set()
    for symbol, _as_of, lookback, horizon, request_json in rows:
        found_symbols.add(str(symbol))
        seen.add((int(lookback), int(horizon), _config_identity(request_json)))

    return ForecastBurden(
        configurations=len(seen),
        forecasts=len(rows),
        symbols=tuple(sorted(found_symbols)),
        start=start,
        end=end,
    )


def _config_identity(request_json: object) -> str:
    """A stable identity for the parts of a config that change what is computed.

    An unreadable request counts as its own configuration rather than collapsing into another.
    Treating unparseable rows as identical would let corruption *reduce* the measured burden,
    and a search that shrinks when data is damaged is the wrong direction for this number to
    move.
    """
    try:
        payload = json.loads(str(request_json))
        config = payload.get("config", {})
        if not isinstance(config, Mapping):
            raise TypeError
    except (ValueError, TypeError):
        return f"unreadable:{hash(str(request_json))}"
    return json.dumps(
        {key: str(config.get(key, "")) for key in SEARCH_DIMENSIONS}, sort_keys=True
    )


def _day_after(value: date) -> str:
    return date.fromordinal(value.toordinal() + 1).isoformat()


__all__ = ["SEARCH_DIMENSIONS", "ForecastBurden", "forecast_burden"]
