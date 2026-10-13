"""Synthetic clock evidence only; never query Windows or a real provider."""

from datetime import UTC, datetime, timedelta
from importlib import import_module, util

import pytest

from quantbot.domain import MarketClock

NOW = datetime(2026, 10, 13, 17, 20, tzinfo=UTC)


def api():
    assert util.find_spec("quantbot.operations.paper_clock") is not None, (
        "shared PAPER clock contract missing"
    )
    return import_module("quantbot.operations.paper_clock")


def sync(**changes):
    return api().SynchronizationHealth(
        checked_at=NOW,
        last_sync_at=NOW - timedelta(minutes=1),
        leap_indicator=0,
        stratum=5,
        last_sync_error=0,
        poll_seconds=1024,
        source="time.windows.com,0x9",
        **changes,
    )


def sample(start, lead=0.17485, duration=0.1):
    before = NOW + timedelta(seconds=start)
    after = before + timedelta(seconds=duration)
    return api().ClockSample(
        wall_before=before,
        wall_after=after,
        monotonic_before=start,
        monotonic_after=start + duration,
        market=MarketClock(
            timestamp=after + timedelta(seconds=lead),
            is_open=True,
            next_open=NOW + timedelta(days=1),
            next_close=NOW.replace(hour=20),
        ),
    )


def assess(samples=None, health=None, *, now=NOW + timedelta(seconds=0.4), mono=0.4):
    return api().assess_paper_clock(
        tuple(samples or [sample(0), sample(0.2)]),
        health or sync(),
        now=now,
        monotonic_now=mono,
        started_monotonic=0,
    )


def test_small_relative_lead_has_raw_evidence_not_clamped_timestamp():
    result = assess()
    assert result.reasons == () and result.evidence is not None
    assert result.evidence.market.timestamp == sample(0.2).market.timestamp
    assert result.evidence.offset_lower_seconds > 0
    assert result.evidence.offset_upper_seconds <= 2
    assert result.evidence.valid_at(NOW + timedelta(seconds=0.4), monotonic_now=0.4)
    assert not result.evidence.valid_at(NOW + timedelta(seconds=121), monotonic_now=121)


def test_verified_evidence_refuses_a_later_wall_clock_step():
    evidence = assess().evidence
    assert evidence is not None
    assert not evidence.valid_at(NOW + timedelta(seconds=1.4), monotonic_now=0.41)
    assert not evidence.valid_at(NOW + timedelta(seconds=0.41), monotonic_now=1.4)


@pytest.mark.parametrize("lead", [-2.000001, 2.000001])
def test_skew_budget_is_not_widened(lead):
    assert assess([sample(0, lead, duration=0), sample(0.2, lead, duration=0)]).reasons


@pytest.mark.parametrize("lead", [-2, 2])
def test_exact_skew_boundary_with_zero_uncertainty(lead):
    assert not assess([sample(0, lead, duration=0), sample(0.2, lead, duration=0)]).reasons


def test_entire_request_bracket_must_fit_budget():
    # The post-response age alone would fit, but request-start uncertainty exceeds it.
    assert assess([sample(0, 1.95), sample(0.2, 1.95)]).reasons


@pytest.mark.parametrize("damage", ["unsynced", "expired", "future", "error", "source", "stratum"])
def test_unverified_synchronization_health_refuses(damage):
    health = sync()
    from dataclasses import replace

    changes = {
        "unsynced": {"leap_indicator": 3},
        "expired": {"last_sync_at": NOW - timedelta(seconds=2049)},
        "future": {"last_sync_at": NOW + timedelta(seconds=1)},
        "error": {"last_sync_error": 1},
        "source": {"source": "Local CMOS Clock"},
        "stratum": {"stratum": 0},
    }[damage]
    assert assess(health=replace(health, **changes)).reasons


@pytest.mark.parametrize(
    "damage", ["replayed", "offset_changed", "reversed", "wall_jump", "budget"]
)
def test_bad_acquisition_is_not_readiness(damage):
    from dataclasses import replace

    first, second = sample(0), sample(0.2)
    if damage == "replayed":
        second = replace(second, market=first.market)
    elif damage == "offset_changed":
        second = sample(0.2, lead=-1)
    elif damage == "reversed":
        second = replace(second, wall_after=second.wall_before - timedelta(seconds=1))
    elif damage == "wall_jump":
        second = replace(second, wall_after=second.wall_after + timedelta(seconds=1))
    result = assess([first, second], mono=5.000001 if damage == "budget" else 0.3)
    assert result.reasons and result.evidence is None


def test_windows_status_parser_does_not_treat_phase_offset_as_broker_measurement():
    text = """Leap Indicator: 0(no warning)
Stratum: 5 (secondary reference - syncd by (S)NTP)
Last Successful Sync Time: 10/13/2026 5:19:00 PM
Source: time.windows.com,0x9
Poll Interval: 10 (1024s)
Phase Offset: 0.2994945s
Last Sync Error: 0 (The command completed successfully.)"""
    health = api().parse_windows_sync_status(text, checked_at=NOW, local_timezone=UTC)
    assert health.last_sync_at == NOW - timedelta(minutes=1)
    assert health.stratum == 5 and health.poll_seconds == 1024
    assert not hasattr(health, "broker_offset_seconds")
    with pytest.raises(ValueError):
        api().parse_windows_sync_status("access denied", checked_at=NOW, local_timezone=UTC)
    nonzero = api().parse_windows_sync_status(
        text.replace("Last Sync Error: 0 ", "Last Sync Error: 0x800705B4 "),
        checked_at=NOW,
        local_timezone=UTC,
    )
    assert nonzero.last_sync_error != 0
    assert assess(health=nonzero).reasons
    with pytest.raises(ValueError):
        api().parse_windows_sync_status(
            text.replace("Last Sync Error: 0 ", "Last Sync Error: 0garbage "),
            checked_at=NOW,
            local_timezone=UTC,
        )


@pytest.mark.parametrize("poll_seconds", [16, 1024])
def test_sync_expiry_during_collection_refuses_assessment(poll_seconds):
    from dataclasses import replace

    limit = min(api().MAX_SYNC_AGE_SECONDS, 2 * poll_seconds)
    health = replace(
        sync(), poll_seconds=poll_seconds, last_sync_at=NOW - timedelta(seconds=limit - 0.2)
    )
    result = assess(health=health)
    assert result.evidence is None
    assert result.reasons == ("PAPER_CLOCK_SYNC_UNVERIFIED",)


@pytest.mark.parametrize("poll_seconds", [16, 1024])
def test_evidence_reuse_expires_at_last_sync_limit(poll_seconds):
    from dataclasses import replace

    limit = min(api().MAX_SYNC_AGE_SECONDS, 2 * poll_seconds)
    health = replace(
        sync(), poll_seconds=poll_seconds, last_sync_at=NOW - timedelta(seconds=limit - 1)
    )
    evidence = assess(health=health).evidence
    assert evidence is not None
    assert evidence.valid_at(NOW + timedelta(seconds=1), monotonic_now=1)
    assert not evidence.valid_at(NOW + timedelta(seconds=1.000001), monotonic_now=1.000001)


def test_collection_does_not_age_away_future_sync_timestamp():
    from dataclasses import replace

    health = replace(sync(), last_sync_at=NOW + timedelta(seconds=0.2))
    result = assess(health=health)
    assert result.evidence is None
    assert result.reasons == ("PAPER_CLOCK_SYNC_UNVERIFIED",)
