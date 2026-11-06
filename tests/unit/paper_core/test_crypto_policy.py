"""Fixed crypto defaults and Eastern risk windows do not reset exposure or losses."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError


def test_crypto_policy_defaults_are_explicit_and_fixed():
    from quantbot.paper_core.crypto_policy import CryptoPaperPolicy

    policy = CryptoPaperPolicy()
    assert policy.model_dump() == {
        "contract": "ALPACA_SPOT_PAPER_V1",
        "symbols": ("BTC/USD", "ETH/USD", "SOL/USD"),
        "estimated_taker_fee_bps": 25,
        "fee_estimate_source": "PROJECT_DEFAULT",
        "sleep_timezone": "America/New_York",
        "sleep_start": "22:00",
        "sleep_end": "06:00",
    }


@pytest.mark.parametrize(
    "symbols",
    [
        (),
        ("BTC/USD", "BTC/USD"),
        ("BTC/USD", "ETH/USD", "SOL/USD", "LTC/USD"),
        ("BTCUSD",),
        ("btc/USD",),
        ("BTC/USDT",),
        ("USD/USD",),
    ],
)
def test_crypto_policy_rejects_unbounded_duplicate_or_non_usd_symbols(symbols):
    from quantbot.paper_core.crypto_policy import CryptoPaperPolicy

    with pytest.raises(ValidationError):
        CryptoPaperPolicy(symbols=symbols)


@pytest.mark.parametrize(
    "change",
    [
        {"estimated_taker_fee_bps": 0},
        {"fee_estimate_source": "BROKER_ACTUAL"},
        {"sleep_start": "23:00"},
        {"sleep_timezone": "UTC"},
    ],
)
def test_crypto_policy_does_not_allow_estimated_cost_or_sleep_contract_override(change):
    from quantbot.paper_core.crypto_policy import CryptoPaperPolicy

    with pytest.raises(ValidationError):
        CryptoPaperPolicy(**change)


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("2026-11-06T02:59:59+00:00", False),  # 21:59:59 ET
        ("2026-11-06T03:00:00+00:00", True),  # 22:00 ET, inclusive
        ("2026-11-06T10:59:59+00:00", True),
        ("2026-11-06T11:00:00+00:00", False),  # 06:00 ET, exclusive
        ("2026-03-08T06:59:59+00:00", True),  # before spring jump
        ("2026-03-08T07:00:00+00:00", True),  # after spring jump
        ("2026-11-01T05:30:00+00:00", True),  # first 01:30 ET
        ("2026-11-01T06:30:00+00:00", True),  # repeated 01:30 ET
    ],
)
def test_sleep_window_uses_eastern_time_and_includes_both_dst_folds(timestamp, expected):
    from quantbot.paper_core.crypto_policy import in_sleep_window, sleep_window

    at = datetime.fromisoformat(timestamp)
    assert in_sleep_window(at) is expected
    window = sleep_window(at)
    assert (window is not None) is expected
    if window is not None:
        assert window[0] <= at < window[1]
        assert window[0].astimezone(ZoneInfo("America/New_York")).hour == 22
        assert window[1].astimezone(ZoneInfo("America/New_York")).hour == 6


@pytest.mark.parametrize(
    ("at", "night_hours", "day_hours"),
    [
        (datetime(2026, 3, 8, 7, tzinfo=UTC), 7, 23),
        (datetime(2026, 11, 1, 6, tzinfo=UTC), 9, 25),
    ],
)
def test_dst_window_and_risk_day_bounds_keep_the_actual_elapsed_duration(
    at,
    night_hours,
    day_hours,
):
    from quantbot.paper_core.crypto_policy import account_risk_day_bounds, sleep_window

    night = sleep_window(at)
    assert night is not None and night[1] - night[0] == timedelta(hours=night_hours)
    start, end = account_risk_day_bounds(at)
    assert start <= at < end
    assert end - start == timedelta(hours=day_hours)
    assert start.astimezone(ZoneInfo("America/New_York")).hour == 0
    assert end.astimezone(ZoneInfo("America/New_York")).hour == 0


def test_utc_midnight_does_not_reset_the_eastern_account_risk_day():
    from quantbot.paper_core.crypto_policy import account_risk_day_bounds

    first = account_risk_day_bounds(datetime(2026, 11, 5, 23, 59, tzinfo=UTC))
    after_midnight = account_risk_day_bounds(datetime(2026, 11, 6, 0, 1, tzinfo=UTC))
    assert (
        first
        == after_midnight
        == (
            datetime(2026, 11, 5, 5, tzinfo=UTC),
            datetime(2026, 11, 6, 5, tzinfo=UTC),
        )
    )


@pytest.mark.parametrize(
    "name",
    [
        "in_sleep_window",
        "sleep_window",
        "account_risk_day_bounds",
        "crypto_session_bounds",
    ],
)
def test_time_helpers_refuse_naive_timestamps(name):
    from quantbot.paper_core import crypto_policy

    with pytest.raises(ValueError, match="aware"):
        getattr(crypto_policy, name)(datetime(2026, 11, 6))


def _overlay_policy():
    from quantbot.paper_core.crypto_policy import CryptoPaperPolicy
    from quantbot.paper_core.entry_policy import EntryPolicy

    return EntryPolicy(
        contract="RESOURCE_BOUNDED_PAPER_V1",
        symbols=(),
        universe="ALPACA_ASSETS_V1",
        crypto=CryptoPaperPolicy(),
    )


def test_sleep_limits_reduce_risk_without_creating_a_one_entry_per_night_limit():
    from quantbot.paper_core.contracts import CoreConfig
    from quantbot.paper_core.economics import EconomicLimits, limits

    config = CoreConfig(account_id="synthetic", symbol="AAPL")
    policy = _overlay_policy()
    daytime = limits(config, entry_policy=policy, at=datetime(2026, 11, 6, 17, tzinfo=UTC))
    nighttime = limits(config, entry_policy=policy, at=datetime(2026, 11, 6, 4, tzinfo=UTC))
    assert daytime == EconomicLimits(None, 50, 9500, 500, 25, 100, 25, 200, 1800, None)
    assert nighttime == EconomicLimits(1, 25, 4750, 500, 12, 100, 12, 100, 1800, None)
    # One held/reserved slot is a concurrency bound; released slots remain reusable.
    assert nighttime.daily_entry_limit is None


@pytest.mark.parametrize("at", [None, datetime(2026, 11, 6, 4)])
def test_crypto_overlay_requires_aware_time_even_when_caller_omits_it(at):
    from quantbot.paper_core.contracts import CoreConfig
    from quantbot.paper_core.economics import limits

    with pytest.raises(ValueError, match="aware"):
        limits(
            CoreConfig(account_id="synthetic", symbol="AAPL"), entry_policy=_overlay_policy(), at=at
        )


def test_absent_overlay_preserves_legacy_and_daytime_limits_without_clock_requirement():
    from quantbot.paper_core.contracts import CoreConfig
    from quantbot.paper_core.economics import EconomicLimits, limits
    from quantbot.paper_core.entry_policy import EntryPolicy

    config = CoreConfig(account_id="synthetic", symbol="AAPL")
    assert limits(config) == EconomicLimits(1, 50, None, 500, 25, 100, None, None, 1800, 6)
    policy = EntryPolicy(contract="RESOURCE_BOUNDED_PAPER_V1", symbols=("AAPL",))
    assert limits(config, entry_policy=policy) == EconomicLimits(
        None,
        50,
        9500,
        500,
        25,
        100,
        25,
        200,
        1800,
        None,
    )


def _market_session(**changes):
    from quantbot.paper_core.crypto_policy import CryptoMarketSession

    payload = dict(
        symbol="BTC/USD",
        session_open=datetime(2026, 11, 6, tzinfo=UTC),
        session_close=datetime(2026, 11, 7, tzinfo=UTC),
        observed_at=datetime(2026, 11, 6, tzinfo=UTC),
        asset_eligibility_id="a" * 64,
    )
    return CryptoMarketSession(**(payload | changes))


def test_crypto_session_at_midnight_uses_new_utc_day_and_normalizes_aware_input():
    session = _market_session(
        observed_at=datetime(2026, 11, 5, 19, tzinfo=ZoneInfo("America/New_York"))
    )
    assert session.observed_at == session.session_open == datetime(2026, 11, 6, tzinfo=UTC)
    assert session.benchmark == "BTC/USD"


@pytest.mark.parametrize(
    "changes",
    [
        {"session_open": datetime(2026, 11, 6, 1, tzinfo=UTC)},
        {"session_close": datetime(2026, 11, 7, 1, tzinfo=UTC)},
        {"observed_at": datetime(2026, 11, 7, tzinfo=UTC)},
        {"observed_at": datetime(2026, 11, 6)},
        {"symbol": "BTCUSD"},
        {"benchmark": "SPY"},
        {"asset_eligibility_id": "synthetic"},
        {"capability_id": "b" * 64},
    ],
)
def test_crypto_session_refuses_wrong_day_unbound_or_synthetic_capability_evidence(changes):
    with pytest.raises(ValidationError):
        _market_session(**changes)
