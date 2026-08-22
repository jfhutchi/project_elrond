"""What a Kronos feature costs the multiple-testing budget (#36, #23).

A forecaster is the canonical search-laundering machine: twenty horizons by ten lookbacks by five
sampling configurations is a thousand data-dependent trials, and if the best-looking one becomes
a feature in a registered hypothesis, the project has performed a thousand and registered one.

The unit is the thing that decides whether Kronos is usable at all, so most of these tests are
about the unit rather than about the arithmetic.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import insert

from quantbot.forecasting.burden import SEARCH_DIMENSIONS, forecast_burden
from quantbot.forecasting.database import ForecastDatabase
from quantbot.forecasting.schema import model_forecasts


@pytest.fixture
def forecasts(tmp_path) -> Iterator[ForecastDatabase]:
    database = ForecastDatabase(tmp_path / "forecast.db")
    yield database
    database.close()


def config(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "model_id": "NeoQuasar/Kronos-small",
        "model_revision": "rev-a",
        "tokenizer_revision": "tok-a",
        "temperature": "0.6",
        "top_p": "0.9",
        "top_k": 0,
        "sample_count": 10,
        "seed": 0,
        "max_context": 512,
        "input_transform_version": "ohlcv-no-amount-v1",
        "integration_version": "int-1",
    }
    base.update(overrides)
    return base


def _burden(database: ForecastDatabase, **kwargs):
    """Run the count inside the database's own transaction, which is its only session API."""
    with database.transaction() as session:
        return forecast_burden(session, **kwargs)


def record(
    database: ForecastDatabase,
    *,
    forecast_id: str,
    symbol: str = "SPY",
    as_of: str = "2026-03-02T00:00:00Z",
    lookback: int = 128,
    horizon: int = 5,
    status: str = "SUCCESS",
    **config_overrides: object,
) -> None:
    """Insert one forecast row directly.

    Written at the row level on purpose: this module reads columns, so the test exercises the
    counting rather than `ForecastRecord`'s construction rules, which have their own tests.
    """
    with database.transaction() as session:
        session.execute(
            insert(model_forecasts).values(
                forecast_id=forecast_id,
                evidence_role="SHADOW",
                forecast_provider="kronos",
                model_id="NeoQuasar/Kronos-small",
                model_revision=str(config_overrides.get("model_revision", "rev-a")),
                tokenizer_id="NeoQuasar/Kronos-Tokenizer-base",
                tokenizer_revision="tok-a",
                kronos_code_revision="code-a",
                integration_version="int-1",
                symbol=symbol,
                as_of=as_of,
                latest_source_bar_at=as_of,
                source_data_hash="d" * 64,
                source_provider="alpaca",
                source_vintage_id="v1",
                source_available_at=as_of,
                source_adjustment_metadata_json="{}",
                source_bar_count=300,
                lookback=lookback,
                horizon=horizon,
                attempt_number=0,
                status=status,
                generated_at=as_of,
                request_json=json.dumps({"config": config(**config_overrides)}),
                result_json='{"ok": true}' if status == "SUCCESS" else None,
                error_code=None if status == "SUCCESS" else "WORKER_FAILED",
            )
        )


def test_one_configuration_run_daily_is_one_trial_not_many(forecasts) -> None:
    """The distinction that decides whether Kronos is usable at all.

    A collector running one configuration every day for a year produces hundreds of forecasts and
    has tested *one* idea about that configuration. Charging a trial per forecast would exhaust
    the budget on a shadow observer that searched nothing.
    """
    for day in range(1, 21):
        record(
            forecasts,
            forecast_id=f"f-{day}",
            as_of=f"2026-03-{day:02d}T00:00:00Z",
        )

    burden = _burden(forecasts)

    assert burden.forecasts == 20
    assert burden.configurations == 1, "the same configuration on twenty days is one idea"


def test_each_swept_dimension_counts_as_a_separate_trial(forecasts) -> None:
    """Sweeping is search, whichever knob is turned.

    Every dimension here changes what the model computes, so each is a distinct hypothesis about
    the market rather than a repetition of the same one.
    """
    record(forecasts, forecast_id="base")
    record(forecasts, forecast_id="lb", lookback=256)
    record(forecasts, forecast_id="hz", horizon=20)
    record(forecasts, forecast_id="temp", temperature="0.9")
    record(forecasts, forecast_id="topp", top_p="0.5")
    record(forecasts, forecast_id="topk", top_k=40)
    record(forecasts, forecast_id="samples", sample_count=30)
    record(forecasts, forecast_id="seed", seed=7)
    record(forecasts, forecast_id="ctx", max_context=256)

    burden = _burden(forecasts)

    assert burden.configurations == 9, "each swept dimension is its own trial"


def test_a_reseeded_rerun_is_a_new_trial(forecasts) -> None:
    """Re-running one configuration under a different seed and keeping the better result is
    search, and it is the cheapest kind to perform by accident."""
    record(forecasts, forecast_id="a", seed=0)
    record(forecasts, forecast_id="b", seed=1)

    assert _burden(forecasts).configurations == 2
    assert "seed" in SEARCH_DIMENSIONS


def test_bumping_a_package_version_is_not_a_new_idea_about_the_market(forecasts) -> None:
    """Provenance that identifies an artifact is not a search dimension.

    Charging a trial for an integration bump would make the burden rise every release, which
    trains people to stop believing the number.
    """
    record(forecasts, forecast_id="a", integration_version="int-1")
    record(forecasts, forecast_id="b", integration_version="int-2")

    assert _burden(forecasts).configurations == 1
    assert "integration_version" not in SEARCH_DIMENSIONS


def test_failed_and_skipped_attempts_still_count(forecasts) -> None:
    """A configuration that was tried and produced nothing still looked at the data.

    Excluding failures would let a search be shrunk by discarding its disappointments, which is
    the original laundering defect wearing different clothes.
    """
    record(forecasts, forecast_id="ok", horizon=5)
    record(forecasts, forecast_id="dead", horizon=20, status="FAILED")

    assert _burden(forecasts).configurations == 2


def test_an_unreadable_request_does_not_shrink_the_burden(forecasts) -> None:
    """A search that gets smaller when data is damaged moves the wrong way.

    Corrupt rows count as their own configuration rather than collapsing into a neighbour.
    """
    record(forecasts, forecast_id="readable")
    for identifier, corrupt in (("bad-a", "{not json"), ("bad-b", "also <<< not json")):
        record(forecasts, forecast_id=identifier, horizon=5)
        with forecasts.transaction() as session:
            session.execute(
                model_forecasts.update()
                .where(model_forecasts.c.forecast_id == identifier)
                .values(request_json=corrupt)
            )

    burden = _burden(forecasts)

    # Three: the readable one, and two differently-corrupt rows that must not collapse into each
    # other. Inflating on corruption is the safe direction; a burden that falls when data is
    # damaged is a budget that can be reduced by breaking things.
    assert burden.configurations == 3, (
        "corrupt configs must not merge with each other or with a real one"
    )


def test_the_window_bounds_are_inclusive_of_the_end_date(forecasts) -> None:
    """`as_of` carries a time, so a plain string comparison would silently drop the last day."""
    record(forecasts, forecast_id="inside", as_of="2026-03-05T14:30:00Z", horizon=5)
    record(forecasts, forecast_id="after", as_of="2026-03-09T00:00:00Z", horizon=20)

    scoped = _burden(forecasts, start=date(2026, 3, 1), end=date(2026, 3, 5)
    )

    assert scoped.forecasts == 1, "the 14:30 forecast on the end date belongs to the window"
    assert scoped.configurations == 1


def test_the_note_says_what_was_counted(forecasts) -> None:
    """A `search_runs` row nobody can interpret later is a number without a provenance."""
    record(forecasts, forecast_id="a", horizon=5)
    record(forecasts, forecast_id="b", horizon=20)

    note = _burden(forecasts).note

    assert "2 distinct Kronos configurations" in note
    assert "2 forecasts" in note
    assert "SPY" in note
