"""Rules that reject rather than resize, and every way one could quietly stop rejecting (#58).

A veto that allows when it cannot measure is indistinguishable from a veto that is not there.
Most of what follows is about that one sentence.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantbot.domain import Position
from quantbot.risk.dependence import DependenceSnapshot, build_dependence_state
from quantbot.risk.exposure import (
    DEFAULT_CLASSIFICATION_PATH,
    build_exposure_state,
    load_exposure_classification,
)
from quantbot.risk.freshness import MarketFreshnessPolicy, expected_freshness
from quantbot.risk.shadow import session_of
from quantbot.risk.vetoes import (
    _EVALUATORS,
    UNIMPLEMENTED,
    PolicyAuthority,
    PortfolioVetoInputs,
    PortfolioVetoPolicy,
    VetoName,
    build_veto_inputs,
    evaluate_portfolio_vetoes,
    session_date_of,
)
from quantbot.strategy import XNYSSession

EASTERN = ZoneInfo("America/New_York")
NOW = datetime(2026, 8, 27, 15, 30, tzinfo=EASTERN).astimezone(UTC)


#: Every implemented veto, so a test exercises the rule it is named after rather than the
#: shipped default's subset. `PortfolioVetoPolicy()` with no arguments is the default, and the
#: two tests about the default construct it directly.
#: Every veto that may actually be enabled. Derived rather than listed: the literal list went
#: stale when SECTOR_CONCENTRATION became EXPOSURE_CONCENTRATION, so "all vetoes" quietly stopped
#: meaning all of them and a test named for completeness was checking a subset.
ALL_VETOES = frozenset(VetoName) - UNIMPLEMENTED


def policy(**overrides: object) -> PortfolioVetoPolicy:
    fields: dict[str, object] = {"enabled": ALL_VETOES}
    fields.update(overrides)
    return PortfolioVetoPolicy(**fields)  # type: ignore[arg-type]



def position(symbol: str, value: str) -> Position:
    amount = Decimal(value)
    return Position(
        symbol=symbol,
        quantity=amount / Decimal("100"),
        market_price=Decimal("100"),
        market_value=amount,
        average_entry_price=Decimal("100"),
    )


#: One exact 120-session window, shared by every snapshot built here. The dates themselves are
#: arbitrary; that they are *the same dates for every symbol* is the invariant #84 added.
SESSION_WINDOW = tuple(date(2026, 3, 2) + timedelta(days=offset) for offset in range(120))


def _snapshot(*symbols: str) -> DependenceSnapshot:
    """A snapshot in which nothing is correlated with anything.

    Part of "nothing objects": `CORRELATION_CONCENTRATION` ships enabled and an absent snapshot
    rejects, so without this every test here would fail for a reason none of them is about. The
    coefficients are deliberately low so no cluster forms and the veto passes.
    """
    # At least three, because `symbols` is derived from coefficient *keys*: a one-symbol
    # snapshot has no pairs, so it would report an empty universe and refuse everything.
    names = sorted({*symbols, "SPY", "TLT", "GLD"})
    return DependenceSnapshot(
        source="test",
        session_window=SESSION_WINDOW,
        coefficients={
            "|".join(sorted((a, b))): Decimal("0.10")
            for i, a in enumerate(names)
            for b in names[i + 1 :]
        },
    )


def _sessions_around(moment: datetime) -> tuple[XNYSSession, ...]:
    """Ten contiguous weekday sessions ending on the Eastern session `moment` belongs to."""
    from quantbot.risk.shadow import session_of

    last = session_of(moment)
    days: list[date] = []
    cursor = last
    while len(days) < 10:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return tuple(
        XNYSSession(
            session_date=day,
            open_at=datetime.combine(day, time(13, 30), tzinfo=UTC),
            close_at=datetime.combine(day, time(20, 0), tzinfo=UTC),
        )
        for day in reversed(days)
    )


def inputs(**overrides: object) -> PortfolioVetoInputs:
    """A portfolio nothing objects to, so each test can break exactly one thing.

    The exposure state is part of "nothing objects" since #75: `EXPOSURE_CONCENTRATION` ships
    enabled, and an absent classification rejects. Leaving it out would have made every test in
    this module fail for a reason none of them are about.
    """
    fields: dict[str, object] = {
        "dependence": build_dependence_state(
            snapshot=_snapshot(str(overrides.get("symbol", "SPY"))),
            symbol=str(overrides.get("symbol", "SPY")),
            positions=[],
            threshold=Decimal("0.90"),
        ),
        # Like the exposure state, the calendar is part of "nothing objects" since #74:
        # `STALE_MARKET_DATA` judges the feed against completed sessions, and no calendar
        # rejects. Without this every test here would fail for a reason none of them is about.
        "freshness": expected_freshness(
            sessions=_sessions_around(NOW),
            observed_at=NOW,
            policy=MarketFreshnessPolicy(),
        ),
        "exposure": build_exposure_state(
            classification=load_exposure_classification(
                Path(__file__).resolve().parents[3] / DEFAULT_CLASSIFICATION_PATH
            ),
            symbol=str(overrides.get("symbol", "SPY")),
            positions=[],
            as_of=date(2026, 8, 27),
        ),
        "symbol": "SPY",
        "equity": Decimal("100000"),
        "committed_net_exposure": Decimal("10000"),
        "order_net_exposure": Decimal("5000"),
        "order_quantity": Decimal("10"),
        "session_date": date(2026, 8, 27),
        "session_opening_equity": Decimal("100000"),
        "latest_market_data_at": NOW - timedelta(seconds=60),
        "observed_at": NOW,
        "average_daily_volume": Decimal("1000000"),
        "volume_sessions": 60,
    }
    fields.update(overrides)
    return PortfolioVetoInputs(**fields)  # type: ignore[arg-type]


def refused(decision) -> set[VetoName]:
    return {item.veto for item in decision.rejections}


def test_a_clean_portfolio_passes_every_enabled_veto() -> None:
    """A gate that refuses everything teaches nothing, so the allowed case is pinned too."""
    decision = evaluate_portfolio_vetoes(inputs(), policy())

    assert decision.allowed
    assert decision.rejections == ()
    assert set(decision.evaluated) == {
        VetoName.NET_EXPOSURE,
        VetoName.DAILY_LOSS,
        VetoName.STALE_MARKET_DATA,
        VetoName.LIQUIDITY,
        VetoName.EXPOSURE_CONCENTRATION,
        VetoName.CORRELATION_CONCENTRATION,
    }
    assert "none refused" in decision.explain()


def test_net_exposure_is_bounded_where_gross_alone_would_not_be() -> None:
    """Sizing already caps gross. A directional book can sit inside gross and breach net."""
    decision = evaluate_portfolio_vetoes(
        inputs(committed_net_exposure=Decimal("99000"), order_net_exposure=Decimal("5000")),
        policy(max_net_exposure_bps=10000),
    )

    assert not decision.allowed
    assert refused(decision) == {VetoName.NET_EXPOSURE}
    assert "exceeds" in decision.rejections[0].reason


def test_a_short_book_nets_down_rather_than_adding_up() -> None:
    """Net is signed. Treating it as gross would refuse a hedged book the limit was fine with."""
    decision = evaluate_portfolio_vetoes(
        inputs(committed_net_exposure=Decimal("-90000"), order_net_exposure=Decimal("50000")),
        policy(enabled=frozenset({VetoName.NET_EXPOSURE}), max_net_exposure_bps=5000),
    )

    assert decision.allowed, "|-90000 + 50000| is 40000, inside a 50% ceiling"


def test_the_daily_loss_limit_stops_entries_for_the_rest_of_the_session() -> None:
    decision = evaluate_portfolio_vetoes(
        inputs(session_opening_equity=Decimal("100000"), equity=Decimal("94000")),
        policy(max_daily_loss_bps=500),
    )

    assert refused(decision) == {VetoName.DAILY_LOSS}
    assert "until the next session" in decision.rejections[0].reason


def test_an_unknown_session_opening_equity_rejects_rather_than_assuming_no_loss() -> None:
    """The fail-open version of this is one `if` away and would be invisible.

    Defaulting the opening equity to the current equity makes the measured loss exactly zero,
    which reads identically to a profitable day. `unmeasured` names the input so an operator
    fixes the cause rather than the threshold.
    """
    decision = evaluate_portfolio_vetoes(inputs(session_opening_equity=None), policy())

    assert refused(decision) == {VetoName.DAILY_LOSS}
    assert decision.rejections[0].unmeasured == "session_opening_equity"


def test_the_daily_loss_session_is_eastern_not_utc() -> None:
    """A UTC-keyed daily limit resets mid-session and halts on the previous day's losses.

    The same off-by-one already cost this project correct fill attribution and correct equity
    marks: a mark written at 20:05 New York is the next UTC day.
    """
    after_close = datetime.combine(date(2026, 8, 27), time(20, 5), tzinfo=EASTERN)
    # Handed over in UTC, which is how a stored timestamp arrives. Passing the Eastern form
    # would let a `moment.date()` implementation pass this test by accident, which is exactly
    # the bug: the two only differ once the value has been normalised to UTC somewhere.
    as_stored = after_close.astimezone(UTC)

    assert as_stored.date() == date(2026, 8, 28), "the UTC date has rolled"
    assert session_date_of(as_stored) == date(2026, 8, 27), "the session has not"
    assert session_date_of(after_close) == date(2026, 8, 27)

    with pytest.raises(ValueError, match="timezone-aware"):
        session_date_of(datetime(2026, 8, 27, 20, 5))


def test_a_missing_session_stops_entries() -> None:
    """Staleness is now a question about sessions, not seconds (#74).

    An observation from two sessions back is stale the moment the last one closes, and this used
    to need an hour-scale ceiling to catch. The calendar catches it without a threshold.
    """
    # `NOW` falls inside the final session, so the last *completed* one is the second to last.
    completed = _sessions_around(NOW)[-2]
    stale_session = _sessions_around(NOW)[-3].close_at
    decision = evaluate_portfolio_vetoes(
        inputs(latest_market_data_at=stale_session),
        policy(enabled=frozenset({VetoName.STALE_MARKET_DATA})),
    )

    assert refused(decision) == {VetoName.STALE_MARKET_DATA}
    rejection = decision.rejections[0]
    assert rejection.reason.startswith("EXPECTED_SESSION_MISSING")
    # The evidence #57 groups on, structured rather than parsed out of prose.
    assert rejection.expected_session == completed.session_date
    assert rejection.observed_session == session_of(stale_session)
    assert rejection.market_state in {"OPEN", "CLOSED"}
    assert rejection.freshness_policy_identity is not None


def test_absent_market_data_is_not_fresh_market_data() -> None:
    decision = evaluate_portfolio_vetoes(
        inputs(latest_market_data_at=None),
        policy(enabled=frozenset({VetoName.STALE_MARKET_DATA})),
    )

    assert refused(decision) == {VetoName.STALE_MARKET_DATA}
    assert decision.rejections[0].unmeasured == "market_data_freshness"


def test_an_absent_calendar_is_not_a_fresh_feed() -> None:
    """Fail closed on missing metadata (#74). No calendar means nothing is owed, which is the
    most permissive possible reading and therefore the one that must reject."""
    decision = evaluate_portfolio_vetoes(
        inputs(freshness=None),
        policy(enabled=frozenset({VetoName.STALE_MARKET_DATA})),
    )

    assert refused(decision) == {VetoName.STALE_MARKET_DATA}
    assert decision.rejections[0].unmeasured == "market_data_freshness"
    assert "FRESHNESS_METADATA_UNAVAILABLE" in decision.rejections[0].reason


def test_an_observation_stamped_in_the_future_is_a_defect_not_freshness() -> None:
    """A future stamp is the freshest possible reading and the clearest possible data fault.

    Comparing only against a ceiling would let a clock skew or a bad ingestion timestamp read as
    the newest data the system has ever seen — permanently, and in the allowing direction.
    """
    decision = evaluate_portfolio_vetoes(
        inputs(latest_market_data_at=NOW + timedelta(days=2)),
        policy(enabled=frozenset({VetoName.STALE_MARKET_DATA})),
    )

    assert refused(decision) == {VetoName.STALE_MARKET_DATA}
    assert decision.rejections[0].unmeasured == "market_data_freshness"


def test_liquidity_bounds_participation_in_average_daily_volume() -> None:
    decision = evaluate_portfolio_vetoes(
        inputs(order_quantity=Decimal("50000"), average_daily_volume=Decimal("1000000")),
        policy(max_participation_bps=100),
    )

    assert refused(decision) == {VetoName.LIQUIDITY}
    assert "average daily volume" in decision.rejections[0].reason


def test_too_little_volume_history_is_not_an_average() -> None:
    """Five sessions of volume is a number, not an average, and a limit computed on one allows."""
    decision = evaluate_portfolio_vetoes(
        inputs(volume_sessions=5), policy(minimum_volume_sessions=20)
    )

    assert refused(decision) == {VetoName.LIQUIDITY}
    assert decision.rejections[0].unmeasured == "volume_sessions"


def test_a_symbol_that_never_trades_is_unbounded_participation() -> None:
    decision = evaluate_portfolio_vetoes(inputs(average_daily_volume=Decimal("0")), policy())

    assert refused(decision) == {VetoName.LIQUIDITY}
    assert "unbounded participation" in decision.rejections[0].reason


def test_every_veto_speaks_even_when_an_earlier_one_refused() -> None:
    """Short-circuiting would make #57 unable to study the vetoes that never got to speak.

    A counterfactual evaluation of whether the liquidity rule adds value cannot run on
    rejections where liquidity was never evaluated because net exposure refused first.
    """
    decision = evaluate_portfolio_vetoes(
        inputs(
            committed_net_exposure=Decimal("200000"),
            session_opening_equity=Decimal("200000"),
            latest_market_data_at=None,
            average_daily_volume=Decimal("0"),
        ),
        policy(),
    )

    assert refused(decision) == {
        VetoName.NET_EXPOSURE,
        VetoName.DAILY_LOSS,
        VetoName.STALE_MARKET_DATA,
        VetoName.LIQUIDITY,
    }
    assert len(decision.evaluated) == 6


def test_a_rejection_carries_what_a_counterfactual_study_needs_to_attribute_it() -> None:
    """#57 asks for `gate_id`, `gate_version` and `rule_parameters_hash` on every rejection.

    Free-text reasons cannot tell a study *which rule at which settings* refused a trade, so the
    study cannot answer whether that rule was worth having.
    """
    strict = policy(max_daily_loss_bps=100)
    loose = policy(max_daily_loss_bps=900)
    breach = inputs(equity=Decimal("97000"))

    tight = evaluate_portfolio_vetoes(breach, strict)
    assert not tight.allowed
    rejection = tight.rejections[0]
    assert rejection.veto is VetoName.DAILY_LOSS
    assert rejection.policy_version == 1
    assert rejection.rule_parameters_hash == strict.identity

    # The same breach under different settings is a different rule, and the hash says so.
    assert evaluate_portfolio_vetoes(breach, loose).allowed
    assert strict.identity != loose.identity


def test_changing_any_limit_changes_the_policy_identity() -> None:
    """Two studies across a threshold change are two studies, and the hash has to know."""
    base = policy()
    for change in (
        {"max_net_exposure_bps": 5000},
        {"max_daily_loss_bps": 250},
        {"freshness": MarketFreshnessPolicy(tolerated_intervals=9)},
        {"max_participation_bps": 50},
        {"minimum_volume_sessions": 40},
        {"enabled": frozenset({VetoName.NET_EXPOSURE})},
        {"version": 2},
    ):
        assert policy(**change).identity != base.identity, change


def test_the_unimplemented_guard_survives_having_nothing_to_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`UNIMPLEMENTED` is empty since #58's correlation rule landed, and the check stays.

    Deleting the guard once the set emptied would remove the mechanism that stops a *future*
    declared name from reading as an enforced control -- which is the whole reason the set
    existed. So the set is provably empty rather than quietly absent, and the guard is driven
    directly to show it still refuses.
    """
    assert UNIMPLEMENTED == frozenset(), "every declared veto now has an evaluator"
    assert set(_EVALUATORS) == set(VetoName), "and the evaluator table covers the enum"

    import quantbot.risk.vetoes as module

    monkeypatch.setattr(module, "UNIMPLEMENTED", frozenset({VetoName.LIQUIDITY}))
    with pytest.raises(ValueError, match="has no evaluator"):
        PortfolioVetoPolicy(enabled=frozenset({VetoName.LIQUIDITY}))


def test_a_disabled_veto_is_not_evaluated_and_the_policy_says_so() -> None:
    """Turning one off is a policy choice, recorded in the identity rather than hidden."""
    only_liquidity = policy(enabled=frozenset({VetoName.LIQUIDITY}))
    decision = evaluate_portfolio_vetoes(
        inputs(session_opening_equity=None, latest_market_data_at=None), only_liquidity
    )

    assert decision.allowed, "the vetoes that would have refused were not enabled"
    assert decision.evaluated == (VetoName.LIQUIDITY,)
    assert only_liquidity.identity != policy().identity


def test_a_veto_never_returns_a_quantity() -> None:
    """The whole distinction from `SizingCapName`: a veto rejects, it does not resize.

    Downsizing an order that breaches a concentration limit still adds the exposure the limit
    exists to bound, so the decision type carries no quantity to be tempted by.
    """
    decision = evaluate_portfolio_vetoes(inputs(order_quantity=Decimal("50000")), policy())

    assert not hasattr(decision, "quantity")
    assert not any(hasattr(item, "quantity") for item in decision.rejections)


def test_every_implemented_veto_is_enabled_and_a_thin_ledger_is_refused() -> None:
    """#75 reverses #58's default, and the cost of doing so is asserted rather than hoped away.

    The old default enabled only the two vetoes a thin ledger could always answer, because
    `DAILY_LOSS` needs a mark from the current session and `LIQUIDITY` needs twenty sessions of
    volume, and on a fresh ledger both refuse. That reasoning was sound and its conclusion was
    wrong: it left tested controls doing nothing while waiting for a threshold preference nobody
    had been asked for, and "which vetoes are enforced" was never a question only a human could
    answer for a paper account.

    So the shipped policy now enables everything implemented, and **a thin ledger is refused**.
    That is the trade, stated here so it cannot be walked back accidentally: a false refusal
    costs a paper entry, a false permit costs the thing the control exists to prevent, and #57
    records every refusal so the choice can be revisited on evidence rather than on taste.
    """
    default = PortfolioVetoPolicy()

    assert default.enabled == ALL_VETOES
    assert VetoName.CORRELATION_CONCENTRATION in default.enabled, (
        "#58's correlation rule landed, so nothing implemented is left disabled"
    )

    thin_ledger = inputs(
        session_opening_equity=None, average_daily_volume=None, volume_sessions=0
    )
    decision = evaluate_portfolio_vetoes(thin_ledger, default)

    assert not decision.allowed, "an enabled veto that cannot measure must reject"
    assert refused(decision) >= {VetoName.DAILY_LOSS, VetoName.LIQUIDITY}

    # Each refusal names what was missing rather than a limit, so an operator reading it knows
    # the fix is data rather than a looser threshold.
    unmeasured = {item.veto: item.unmeasured for item in decision.rejections}
    assert unmeasured[VetoName.DAILY_LOSS] == "session_opening_equity"
    assert unmeasured[VetoName.LIQUIDITY] == "average_daily_volume"


def test_an_absent_dependence_snapshot_refuses_on_its_own() -> None:
    """Fail closed, asserted in isolation (#58).

    The thin-ledger test above checks a *superset* of refusals, so it stays green whether or not
    correlation refuses -- which makes it useless for this property. Only this veto is enabled
    here, so the refusal can only have come from the missing snapshot.
    """
    decision = evaluate_portfolio_vetoes(
        inputs(dependence=None),
        policy(enabled=frozenset({VetoName.CORRELATION_CONCENTRATION})),
    )

    assert refused(decision) == {VetoName.CORRELATION_CONCENTRATION}
    assert decision.rejections[0].unmeasured == "dependence_snapshot"


def test_a_holding_the_snapshot_never_measured_refuses() -> None:
    """The expensive case: the order is measurable and a *held* position is not.

    Every cluster total that includes the unmeasured symbol is then an understatement, and an
    understated numerator is the permissive direction. A naive implementation proceeds here
    because the order itself looks fine.
    """
    measured = _snapshot("SPY")
    state = build_dependence_state(
        snapshot=measured,
        symbol="SPY",
        positions=[position("NVDA", "9000")],
        threshold=Decimal("0.90"),
    )
    assert state.unmeasured == ("NVDA",)

    decision = evaluate_portfolio_vetoes(
        inputs(dependence=state),
        policy(enabled=frozenset({VetoName.CORRELATION_CONCENTRATION})),
    )

    assert refused(decision) == {VetoName.CORRELATION_CONCENTRATION}
    assert decision.rejections[0].unmeasured == "UNMEASURED_SYMBOL"
    assert "NVDA" in decision.rejections[0].reason


def test_a_correlated_cluster_over_its_ceiling_refuses_and_names_the_snapshot() -> None:
    """The rule doing its actual job, plus the evidence #57 groups refusals on."""
    correlated = DependenceSnapshot(
        source="test",
        session_window=SESSION_WINDOW,
        coefficients={"QQQ|SPY": Decimal("0.95")},
    )
    state = build_dependence_state(
        snapshot=correlated,
        symbol="SPY",
        positions=[position("QQQ", "38000")],
        threshold=Decimal("0.90"),
    )

    decision = evaluate_portfolio_vetoes(
        inputs(dependence=state),
        policy(enabled=frozenset({VetoName.CORRELATION_CONCENTRATION})),
    )

    assert refused(decision) == {VetoName.CORRELATION_CONCENTRATION}
    rejection = decision.rejections[0]
    assert "QQQ, SPY" in rejection.reason
    assert rejection.unmeasured is None, "a breached ceiling is not a missing measurement"
    assert rejection.dependence_snapshot_identity == correlated.identity


def test_the_shipped_thresholds_are_the_systems_and_say_so() -> None:
    """A threshold an agent selected is not operator-approved (#72).

    `authority` is deliberately outside `identity`: two policies with the same numbers behave
    identically and must group as one dataset in a counterfactual study, so provenance is
    reported beside the identity rather than folded into it.
    """
    default = PortfolioVetoPolicy()

    assert default.authority is PolicyAuthority.PROJECT_DEFAULT

    adopted = PortfolioVetoPolicy(authority=PolicyAuthority.OPERATOR_SET)
    assert adopted.identity == default.identity, (
        "an operator adopting the same numbers does not change which numbers they are"
    )
    assert adopted.authority is not default.authority


def test_the_current_session_close_is_fresh() -> None:
    """The veto must be able to pass, or every staleness test above proves only that it refuses.

    The calendar cases -- weekend, long weekend, holiday, half-day, missing session, intraday
    stoppage -- live in `tests/unit/risk/test_freshness.py`, where they can be written against
    the rule directly instead of through a portfolio fixture.
    """
    # The last *completed* session's close. The final session is still open at `NOW`, so its
    # close is in the future -- which is a data defect, not freshness.
    current = _sessions_around(NOW)[-2].close_at
    decision = evaluate_portfolio_vetoes(
        inputs(latest_market_data_at=current),
        policy(enabled=frozenset({VetoName.STALE_MARKET_DATA})),
    )

    assert decision.allowed, decision.explain()


def test_a_vetoed_order_reserves_nothing() -> None:
    """A refused order must not consume the capacity it was refused for.

    This is the shape of the defect this pairing had when first written: the runner appended the
    risk reservation before evaluating the vetoes, so a veto on the first symbol silently starved
    every symbol evaluated after it in the same cycle — a position slot and its gross exposure
    held for an order that was never going to be submitted.

    The veto layer itself carries no reservation and never has; this pins the contract the
    runner depends on, so a future edit that reintroduces the ordering has something to break.
    """
    decision = evaluate_portfolio_vetoes(
        inputs(order_quantity=Decimal("50000")), policy()
    )

    assert not decision.allowed
    assert not hasattr(decision, "reservation")
    # The decision is a verdict about one order and says nothing about capacity, which is what
    # makes "reserve only after it allows" the caller's obligation rather than a shared secret.
    assert set(decision.model_dump()) == {
        "symbol",
        "policy_version",
        "rule_parameters_hash",
        "evaluated",
        "rejections",
    }


def test_volume_history_is_counted_in_sessions_not_rows() -> None:
    """Two bars for one day is one session of history, not two.

    Counting rows would overstate the history whenever a feed carries more than one bar for a
    day, and the liquidity veto's whole question is how many *sessions* the average reaches
    back. Overstating it lets the veto pass on a history too short to be an average.
    """
    day = datetime(2026, 8, 26, 16, 0, tzinfo=EASTERN)
    duplicated = [
        (day, Decimal("400000")),
        (day.replace(hour=12), Decimal("600000")),
        (day - timedelta(days=1), Decimal("1000000")),
    ]

    built = build_veto_inputs(
        symbol="SPY",
        equity=Decimal("100000"),
        positions=[],
        order_quantity=Decimal("10"),
        order_price=Decimal("500"),
        equity_marks=[],
        symbol_bars=duplicated,
        observed_at=NOW,
        volume_sessions=20,
    )

    assert built.volume_sessions == 2, "three rows across two sessions is two sessions"
    # And the duplicated day's volume is its total, not one of the two rows.
    assert built.average_daily_volume == Decimal("1000000")


def test_a_new_symbol_entry_cannot_clear_net_exposure_on_a_fabricated_zero() -> None:
    """The fail-open GPT-5.6 Sol found in #60, pinned so it cannot come back.

    The paper-smoke path substituted `Decimal("0")` as the reference price when the symbol was
    not already held. `build_veto_inputs` then computed `order_net_exposure = quantity * 0`, so a
    real proposed entry contributed **nothing** to projected exposure while `NET_EXPOSURE`
    reported itself as evaluated — a fail-open inside the one subsystem whose stated rule is that
    a missing risk input rejects.

    Zero is not a conservative substitute for an unknown price. It is the single most permissive
    value available for this term.
    """
    # A book already at the ceiling. A genuinely zero-notional order would sit inside it.
    at_the_limit = inputs(
        committed_net_exposure=Decimal("100000"),
        order_net_exposure=Decimal("0"),
    )
    assert evaluate_portfolio_vetoes(at_the_limit, policy(max_net_exposure_bps=10000)).allowed, (
        "a zero-notional order is inside the ceiling, which is exactly why zero was dangerous"
    )

    # The same book, with the price genuinely unmeasured, must refuse.
    unmeasured = inputs(
        committed_net_exposure=Decimal("100000"),
        order_net_exposure=None,
    )
    decision = evaluate_portfolio_vetoes(unmeasured, policy(max_net_exposure_bps=10000))

    assert not decision.allowed
    # Both refuse, and for the same missing input: a concentration limit cannot attribute an
    # order to a group without knowing what the order is worth either. A missing price is not a
    # zero one in either rule.
    assert refused(decision) == {
        VetoName.NET_EXPOSURE,
        VetoName.EXPOSURE_CONCENTRATION,
        VetoName.CORRELATION_CONCENTRATION,
    }
    assert {
        item.unmeasured for item in decision.rejections
    } == {"REFERENCE_PRICE_UNAVAILABLE"}
    assert decision.rejections[0].unmeasured == "REFERENCE_PRICE_UNAVAILABLE"
    assert "not a zero one" in decision.rejections[0].reason


def test_the_builder_propagates_a_missing_price_rather_than_collapsing_it() -> None:
    """`build_veto_inputs` is where the substitution happened, so it is where this is pinned.

    A caller with no reference price has an unmeasured input. The builder must carry that
    through as `None` so the veto can refuse, rather than multiplying the quantity by a stand-in
    and handing the veto a number that looks measured.
    """
    without_price = build_veto_inputs(
        symbol="SPY",
        equity=Decimal("100000"),
        positions=[],
        order_quantity=Decimal("1"),
        order_price=None,
        equity_marks=[],
        symbol_bars=[],
        observed_at=NOW,
        volume_sessions=20,
    )
    assert without_price.order_net_exposure is None

    with_price = build_veto_inputs(
        symbol="SPY",
        equity=Decimal("100000"),
        positions=[],
        order_quantity=Decimal("2"),
        order_price=Decimal("500"),
        equity_marks=[],
        symbol_bars=[],
        observed_at=NOW,
        volume_sessions=20,
    )
    assert with_price.order_net_exposure == Decimal("1000")
