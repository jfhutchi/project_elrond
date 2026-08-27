"""Rules that reject rather than resize, and every way one could quietly stop rejecting (#58).

A veto that allows when it cannot measure is indistinguishable from a veto that is not there.
Most of what follows is about that one sentence.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quantbot.risk.vetoes import (
    UNIMPLEMENTED,
    PortfolioVetoInputs,
    PortfolioVetoPolicy,
    VetoName,
    build_veto_inputs,
    evaluate_portfolio_vetoes,
    session_date_of,
)

EASTERN = ZoneInfo("America/New_York")
NOW = datetime(2026, 8, 27, 15, 30, tzinfo=EASTERN).astimezone(UTC)


#: Every implemented veto, so a test exercises the rule it is named after rather than the
#: shipped default's subset. `PortfolioVetoPolicy()` with no arguments is the default, and the
#: two tests about the default construct it directly.
ALL_VETOES = frozenset(
    {
        VetoName.NET_EXPOSURE,
        VetoName.DAILY_LOSS,
        VetoName.STALE_MARKET_DATA,
        VetoName.LIQUIDITY,
    }
)


def policy(**overrides: object) -> PortfolioVetoPolicy:
    fields: dict[str, object] = {"enabled": ALL_VETOES}
    fields.update(overrides)
    return PortfolioVetoPolicy(**fields)  # type: ignore[arg-type]


def inputs(**overrides: object) -> PortfolioVetoInputs:
    """A portfolio nothing objects to, so each test can break exactly one thing."""
    fields: dict[str, object] = {
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
        policy(max_net_exposure_bps=5000),
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


def test_stale_market_data_stops_entries() -> None:
    decision = evaluate_portfolio_vetoes(
        inputs(latest_market_data_at=NOW - timedelta(seconds=3600)),
        policy(max_market_data_age_seconds=900),
    )

    assert refused(decision) == {VetoName.STALE_MARKET_DATA}
    assert "3600s old" in decision.rejections[0].reason


def test_absent_market_data_is_not_fresh_market_data() -> None:
    decision = evaluate_portfolio_vetoes(inputs(latest_market_data_at=None), policy())

    assert refused(decision) == {VetoName.STALE_MARKET_DATA}
    assert decision.rejections[0].unmeasured == "latest_market_data_at"


def test_an_observation_stamped_in_the_future_is_a_defect_not_freshness() -> None:
    """Age below zero is the freshest possible reading and the clearest possible data fault.

    Comparing only against the ceiling would let a clock skew or a bad ingestion timestamp read
    as the newest data the system has ever seen — permanently, and in the allowing direction.
    """
    decision = evaluate_portfolio_vetoes(
        inputs(latest_market_data_at=NOW + timedelta(seconds=120)), policy()
    )

    assert refused(decision) == {VetoName.STALE_MARKET_DATA}
    assert decision.rejections[0].unmeasured == "latest_market_data_at"


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
    assert len(decision.evaluated) == 4


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
        {"max_market_data_age_seconds": 60},
        {"max_participation_bps": 50},
        {"minimum_volume_sessions": 40},
        {"enabled": frozenset({VetoName.NET_EXPOSURE})},
        {"version": 2},
    ):
        assert policy(**change).identity != base.identity, change


def test_an_unimplemented_veto_cannot_be_enabled() -> None:
    """Correlation has no evaluator, and a veto that reads as enforced is worse than a missing one.

    It is declared in `VetoName` so the gap is a visible member of the enum rather than an
    absence a reader has to notice, and refused at configuration so nothing can believe the
    portfolio is protected against a concentration nothing measures.

    `EXPOSURE_CONCENTRATION` left this set when #59's classification landed; a name that stayed
    here after gaining an evaluator would refuse to be enabled forever.
    """
    assert UNIMPLEMENTED == {VetoName.CORRELATION_CONCENTRATION}
    for name in UNIMPLEMENTED:
        with pytest.raises(ValueError, match="has no evaluator"):
            policy(enabled=frozenset({VetoName.NET_EXPOSURE, name}))


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


def test_the_shipped_default_enables_only_what_a_thin_ledger_can_answer() -> None:
    """The default must protect without silently halting the paper daemon (#58).

    `DAILY_LOSS` needs a mark from the current session and `LIQUIDITY` needs twenty sessions of
    volume. On a fresh or thin ledger neither exists, so both refuse — correctly, since an
    enabled veto that cannot measure must reject — and enabling them by default would stop the
    entries this project needs in order to accumulate forward evidence at all.

    Which vetoes are enforced is therefore **operator policy**, exactly as the economic mandate
    is. The default enables the two that are always answerable; the other two are implemented,
    tested, and off until the operator turns them on.
    """
    default = PortfolioVetoPolicy()

    assert default.enabled == {VetoName.NET_EXPOSURE, VetoName.STALE_MARKET_DATA}

    thin_ledger = inputs(
        session_opening_equity=None, average_daily_volume=None, volume_sessions=0
    )
    assert evaluate_portfolio_vetoes(thin_ledger, default).allowed, (
        "the shipped default must not halt entries on a ledger with no history"
    )

    # And the two that are off are off because they would refuse, not because they are broken.
    everything = policy(enabled=ALL_VETOES)
    refused_now = evaluate_portfolio_vetoes(thin_ledger, everything)
    assert refused(refused_now) == {VetoName.DAILY_LOSS, VetoName.LIQUIDITY}


def test_the_staleness_default_fits_the_daily_bars_this_system_trades() -> None:
    """A 900-second ceiling refused every entry, and that was a bug in the rule (#58).

    This strategy decides once per session on daily bars, so the newest observation is always
    the previous session's close — about a day old at decision time, and longer across a
    weekend. The first version of this veto encoded an intraday assumption and blocked the wired
    cycle entirely. Four days clears a long weekend and still catches a feed genuinely behind.
    """
    default = PortfolioVetoPolicy()
    yesterday = inputs(latest_market_data_at=NOW - timedelta(days=1))
    long_weekend = inputs(latest_market_data_at=NOW - timedelta(days=3))
    behind = inputs(latest_market_data_at=NOW - timedelta(days=9))

    assert evaluate_portfolio_vetoes(yesterday, default).allowed
    assert evaluate_portfolio_vetoes(long_weekend, default).allowed
    assert refused(evaluate_portfolio_vetoes(behind, default)) == {VetoName.STALE_MARKET_DATA}


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
    assert refused(decision) == {VetoName.NET_EXPOSURE}
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
