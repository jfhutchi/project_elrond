"""Synthetic accounting checks, never evidence of market profitability."""

from dataclasses import replace
from decimal import Decimal

import pytest

from quantbot.domain import Bar
from quantbot.learner.data import DailyInput
from quantbot.learner.metrics import ScoredRow
from tests.unit.learner.test_data import session_sequence


def panel():
    calendar = session_sequence(13)
    rows = [ScoredRow(str(i) + s, str(i), s, calendar.sessions[i].close_at,
                     calendar.sessions[i + 5].close_at, score, 0., 0., 0.)
            for i in (0, 1, 5) for s, score in (("A", 2.), ("B", 1.))]
    inputs = tuple(DailyInput(bar=Bar(
        symbol=s, timestamp=t.close_at, open="100", high="200", low="50",
        close="110" if s == "A" else "100", volume="1000000", adjustment="1",
    ), available_at=t.close_at) for t in calendar.sessions for s in ("A", "B", "SPY"))
    return calendar, rows, inputs


def test_next_open_two_sided_costs_matched_baselines_and_nonoverlap():
    from quantbot.learner.economics import simulate

    calendar, rows, inputs = panel()
    result = simulate(rows, inputs, calendar, expected_count=len(rows))
    assert result["period_count"] == 2
    expected = (1.1 * (1 - .0005) / (1 + .0005)) ** 2 - 1
    assert result["portfolios"]["top_quintile"]["net_return"] == pytest.approx(expected)
    market = ((1 - .0005) / (1 + .0005)) ** 2 - 1
    assert result["portfolios"]["spy"]["net_return"] == pytest.approx(market)
    assert result["portfolios"]["top_quintile"]["excess_return_vs_spy"] == pytest.approx(
        expected - market)
    assert result["portfolios"]["top_quintile"]["traded_notional_over_initial"] > 4
    assert result["periods"][0]["entry_at"] == calendar.sessions[1].open_at.isoformat()
    assert result["positive_expectancy_justified"] is False


def test_missing_coverage_calendar_or_duplicate_prices_refuse():
    from quantbot.learner.economics import simulate

    calendar, rows, inputs = panel()
    with pytest.raises(ValueError, match="complete"):
        simulate(rows[:-1], inputs, calendar, expected_count=len(rows))
    with pytest.raises(ValueError, match="missing"):
        simulate(rows, inputs[4:], calendar, expected_count=len(rows))
    with pytest.raises(ValueError, match="duplicate"):
        simulate(rows, (*inputs, inputs[0]), calendar, expected_count=len(rows))
    with pytest.raises(ValueError, match="five"):
        simulate([replace(r, target_at=calendar.sessions[6].close_at) for r in rows[:2]],
                 inputs, calendar, expected_count=2)


def test_constant_scores_select_all_and_daily_drawdown_is_measured():
    from quantbot.learner.economics import simulate

    calendar, rows, inputs = panel()
    rows = [replace(r, score=1.) for r in rows]
    changed = tuple(item.model_copy(update={"bar": item.bar.model_copy(update={
        "close": Decimal("50")})}) if item.bar.timestamp == calendar.sessions[2].close_at
        else item for item in inputs)
    result = simulate(rows, changed, calendar, expected_count=len(rows))
    assert result["portfolios"]["top_quintile"] == result["portfolios"]["all_universe"]
    assert result["portfolios"]["top_quintile"]["max_drawdown"] > .5


def test_report_cannot_overwrite_prior_evidence(tmp_path):
    from quantbot.learner.economics import write_report

    path = tmp_path / "report.json"
    write_report(path, {"result": "negative"})
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_report(path, {"result": "positive"})
    assert path.read_bytes() == before
