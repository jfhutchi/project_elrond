import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from quantbot.learner.consent import PaperConsent, consent_recorded
from quantbot.learner.progress import monitor_progress
from quantbot.operations.alerts import AlertStore

NOW = datetime(2026, 9, 30, 21, 30, tzinfo=UTC)


def document():
    return dict(schema='ELROND_PAPER_CONSENT_V1', mode='PAPER', model_id='a' * 64,
                recorded_at=NOW, authority='EXPLICIT_OPERATOR_CHAT_APPROVAL',
                maximum_new_positions=1, maximum_position_bps=500, maximum_entry_risk_bps=25,
                stop_bps=100, horizon_minutes=30,
                inherited_positions='MONITOR_ONLY_EXISTING_EXIT_OWNER')


def test_consent_matches_published_strategy_without_execution_authority(tmp_path, monkeypatch):
    monkeypatch.setattr('quantbot.learner.forward._models',
                        lambda _: [SimpleNamespace(model_id='a' * 64)])
    (tmp_path / 'paper-consent.json').write_text(
        PaperConsent(**document()).model_dump_json(by_alias=True))
    assert consent_recorded(tmp_path, now=NOW)
    assert not consent_recorded(tmp_path, now=NOW - timedelta(seconds=1))
    monkeypatch.setattr('quantbot.learner.forward._models',
                        lambda _: [SimpleNamespace(model_id='b' * 64)])
    assert not consent_recorded(tmp_path, now=NOW)
    assert not (tmp_path / 'paper-execution.json').exists()


def test_other_configured_model_does_not_inherit_recorded_consent(tmp_path, monkeypatch):
    monkeypatch.setattr('quantbot.learner.forward._models',
                        lambda _: [SimpleNamespace(model_id=x * 64) for x in ('a', 'b')])
    (tmp_path / 'paper-consent.json').write_text(
        PaperConsent(**document()).model_dump_json(by_alias=True))
    (tmp_path / 'paper-execution.json').write_text(json.dumps({'model_id': 'b' * 64}))
    assert not consent_recorded(tmp_path, now=NOW)


@pytest.mark.parametrize('change', [dict(mode='LIVE'), dict(maximum_new_positions=2),
                                  dict(recorded_at=NOW.replace(tzinfo=None)),
                                  dict(trading_enabled=True)])
def test_invalid_or_expanded_consent_rejected(change):
    with pytest.raises(ValueError):
        PaperConsent(**(document() | change))


def test_recorded_consent_is_setup_blocker_not_ai_choice_or_promotion(tmp_path):
    receipt = dict(status='BLOCKED', operator_consent='RECORDED', trading_enabled=False,
                   promotion='NONE', blockers=['PAPER_PROMOTION_REQUIRED'], intraday={})
    with AlertStore(tmp_path / 'alerts.db') as store:
        result = monitor_progress(store, receipt, now=NOW)
        assert result['paper_status'] == 'SETUP_INCOMPLETE_CONSENT_RECORDED'
        assert receipt['promotion'] == 'NONE'
        assert receipt['trading_enabled'] is False
        assert receipt['blockers'] == ['PAPER_PROMOTION_REQUIRED']
        assert result['active_alerts'][0]['action'].startswith('Paper trading setup incomplete.')
