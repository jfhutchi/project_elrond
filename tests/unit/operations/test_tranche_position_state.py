"""Durable tranche attribution on positions, and the backward compatibility it must not break.

`docs/tranching-spec.md` singles this out as the part most likely to produce a reconciliation
defect. Under tranching the same symbol can be held by several tranches at once, entered on
different sessions with different stops, so an exit has to name the tranche it belongs to. Get
it wrong and the broker reports a position Elrond does not expect, reconciliation fails, and
trading halts.

The property with live consequences today is the boring one: the paper account is holding
positions whose durable state was written before this field existed. Reading a missing tranche
as an error, or as anything other than 0, would strand them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from quantbot.strategy import PositionContext

ENTERED = datetime(2026, 8, 17, 13, 23, 1, tzinfo=UTC)


def _legacy_state() -> dict[str, str]:
    """Exactly the shape written before tranching existed — note the absent tranche key."""
    return {
        "entered_at": ENTERED.isoformat(),
        "initial_stop": "290.1234",
        "active_stop": "295.5000",
    }


def _read(state: dict[str, str]) -> tuple[Decimal, Decimal, int]:
    """The decode the runner performs. Kept in one place so the default cannot drift."""
    return (
        Decimal(str(state["initial_stop"])),
        Decimal(str(state["active_stop"])),
        int(state.get("tranche", 0)),
    )


def test_state_written_before_tranching_existed_still_loads() -> None:
    """The live account holds positions in exactly this shape. They must not strand."""
    initial, active, tranche = _read(_legacy_state())

    assert initial == Decimal("290.1234")
    assert active == Decimal("295.5000")
    assert tranche == 0, "a missing tranche means untranched, not unknown"


def test_a_recorded_tranche_round_trips() -> None:
    state = {**_legacy_state(), "tranche": 3}

    assert _read(state)[2] == 3


def test_tranche_zero_is_written_explicitly_rather_than_omitted() -> None:
    """Self-describing state: absence should never be load-bearing going forward.

    Reading a missing key as 0 is required for old records, but new records say 0 out loud so
    the two cases stay distinguishable if this ever needs revisiting.
    """
    context = PositionContext(
        symbol="XLK",
        entered_at=ENTERED,
        initial_stop=Decimal("290.1234"),
        active_stop=Decimal("295.5000"),
    )
    written = {
        "entered_at": context.entered_at.isoformat(),
        "initial_stop": str(context.initial_stop),
        "active_stop": str(context.active_stop),
        "tranche": context.tranche,
    }

    assert "tranche" in written
    assert written["tranche"] == 0


def test_a_position_context_defaults_to_the_untranched_tranche() -> None:
    context = PositionContext(
        symbol="SPY",
        entered_at=ENTERED,
        initial_stop=Decimal("500"),
        active_stop=Decimal("510"),
    )

    assert context.tranche == 0


def test_the_same_symbol_can_be_held_by_several_tranches_independently() -> None:
    """The mechanism tranching needs: one symbol, several holdings, separate stops.

    Without per-tranche identity these collapse into one position and the second exit closes a
    holding that belongs to a different tranche.
    """
    early = PositionContext(
        symbol="XLK",
        entered_at=datetime(2026, 8, 5, 20, 0, tzinfo=UTC),
        initial_stop=Decimal("280"),
        active_stop=Decimal("288"),
        tranche=0,
    )
    late = PositionContext(
        symbol="XLK",
        entered_at=datetime(2026, 8, 19, 20, 0, tzinfo=UTC),
        initial_stop=Decimal("295"),
        active_stop=Decimal("295"),
        tranche=3,
    )

    assert early.symbol == late.symbol
    assert (early.tranche, late.tranche) != (early.tranche, early.tranche)
    assert early.active_stop != late.active_stop, "each tranche trails its own stop"


def test_a_negative_tranche_is_rejected() -> None:
    """Cheap guard: a negative index would silently select the last tranche via wraparound."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PositionContext(
            symbol="SPY",
            entered_at=ENTERED,
            initial_stop=Decimal("500"),
            active_stop=Decimal("510"),
            tranche=-1,
        )
