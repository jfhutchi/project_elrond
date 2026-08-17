"""Selecting the session sequence from the config, not from the call site.

The sequence carries a calendar label that MonthlyRoster validates against. Using the XNYS
builder on a 24/7 strategy therefore fails during roster-window validation — far from the
mistake, with a message about roster schedules rather than calendars. `sequence_for` exists to
make that mismatch unrepresentable, and these tests pin both halves: it picks correctly, and
the mismatch it prevents is real.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from quantbot.market_data.calendar import (
    crypto_session_sequence,
    crypto_sessions,
    session_closes,
    session_sequence,
)
from quantbot.operations.runners import sequence_for
from quantbot.strategy import StrategyConfig, load_strategy_config

ROOT = Path(__file__).parents[2]
EQUITY = ROOT / "config" / "strategy-v1-2.yaml"
CRYPTO = ROOT / "config" / "strategy-crypto-v2.yaml"


def _crypto_days() -> tuple[Any, ...]:
    return crypto_sessions(date(2026, 7, 1), date(2026, 9, 5))


def test_a_crypto_config_selects_the_crypto_sequence() -> None:
    config = load_strategy_config(CRYPTO)

    sequence = sequence_for(config, _crypto_days())

    assert sequence.calendar == "CRYPTO24"


def test_an_equity_config_selects_the_xnys_sequence() -> None:
    config = load_strategy_config(EQUITY)
    assert config.calendar == "XNYS"

    sequence = sequence_for(config, _crypto_days())

    # The builder follows the config, not the shape of the sessions handed to it.
    assert sequence.calendar == "XNYS"


def test_the_mismatch_sequence_for_prevents_is_real() -> None:
    """A roster built on one calendar is rejected by a sequence labelled another."""
    sessions = _crypto_days()
    crypto = crypto_session_sequence(sessions)
    xnys = session_sequence(sessions)
    first_august = next(
        i for i, s in enumerate(sessions) if s.session_date == date(2026, 8, 1)
    )
    config = load_strategy_config(CRYPTO)
    payload = config.model_dump(mode="python")
    payload["universe"] = ("BTC/USD",)
    payload["roster_size"] = 1
    single = StrategyConfig.model_validate(payload)

    from quantbot.strategy import MonthlyRoster

    roster = MonthlyRoster(
        calendar="CRYPTO24",
        evaluated_at=sessions[first_august - 1].close_at,
        effective_at=sessions[first_august].open_at,
        expires_at=crypto.roster_expiry_after(first_august),
        symbols=(),
        rankings=(),
    )
    assert single.calendar == "CRYPTO24"

    # The correctly-labelled sequence accepts it.
    crypto.validate_roster_window(roster, sessions[first_august].open_at)

    # The mismatched one does not, and the message is about rosters rather than calendars.
    with pytest.raises(ValueError, match="calendar does not match"):
        xnys.validate_roster_window(roster, sessions[first_august].open_at)


def test_session_closes_defaults_to_xnys_and_carries_a_calendar_when_asked() -> None:
    sessions = _crypto_days()

    default = session_closes(sessions)
    crypto = session_closes(sessions, calendar="CRYPTO24")

    assert {c.calendar for c in default} == {"XNYS"}
    assert {c.calendar for c in crypto} == {"CRYPTO24"}
    assert [c.close_at for c in crypto] == [s.close_at for s in sessions]
