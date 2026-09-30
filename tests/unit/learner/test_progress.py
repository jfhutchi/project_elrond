from datetime import UTC, datetime, timedelta

from quantbot.learner.progress import monitor_progress
from quantbot.operations.alerts import AlertPolicy, AlertStore

NOW = datetime(2026, 10, 1, 14, 10, tzinfo=UTC)


def receipt(*, blockers=(), progress=0, expected=True, session="2026-10-01"):
    return dict(
        status="BLOCKED",
        resolved_this_run=0,
        evaluated_this_run=0,
        blockers=list(blockers),
        intraday=dict(
            progress_expected=expected,
            progress_session=session,
            observations_this_run=progress,
            predictions_this_run=progress,
            outcomes_this_run=0,
            trained_this_run=0,
            captured_bars=999,
        ),
    )


def test_capture_and_successful_exit_are_not_productive_progress(tmp_path):
    with AlertStore(
        tmp_path / "alerts.db", policy=AlertPolicy(warning_seconds=600, critical_seconds=1200)
    ) as store:
        first = monitor_progress(store, receipt(), now=NOW)
        assert first["forecast_status"] == "AWAITING_PROGRESS"
        warning = monitor_progress(store, receipt(), now=NOW + timedelta(minutes=10))
        assert warning["forecast_status"] == "NO_PROGRESS_WARNING"
        critical = monitor_progress(store, receipt(), now=NOW + timedelta(minutes=20))
        assert critical["forecast_status"] == "NO_PROGRESS_CRITICAL"
        assert any(a["code"] == "LEARNER_NO_PROGRESS" for a in critical["active_alerts"])
        recovered = monitor_progress(store, receipt(progress=2), now=NOW + timedelta(minutes=30))
        assert recovered["forecast_status"] == "PROGRESSING"
        assert recovered["last_progress_at"] == (NOW + timedelta(minutes=30)).isoformat()
        assert any(e["event"] == "RECOVERED" for e in recovered["notifications"])


def test_protected_session_and_warmup_waits_do_not_accumulate_stall_time(tmp_path):
    with AlertStore(tmp_path / "alerts.db") as store:
        for i, code in enumerate(
            ("PROTECTED_FEATURE_WINDOW", "OUTSIDE_FORECAST_SESSION", "FORECAST_LOOKBACK_WARMUP")
        ):
            report = monitor_progress(
                store, receipt(blockers=[code], expected=False), now=NOW + timedelta(days=i)
            )
            assert report["forecast_status"] == "EXPECTED_WAIT"
            assert not report["active_alerts"]
        resumed = monitor_progress(
            store, receipt(session="2026-10-05"), now=NOW + timedelta(days=4)
        )
        assert resumed["forecast_status"] == "AWAITING_PROGRESS"
        assert resumed["expected_idle_seconds"] == 0


def test_paper_gates_do_not_impersonate_forecast_approval_or_evidence(tmp_path):
    with AlertStore(tmp_path / "alerts.db") as store:
        report = monitor_progress(
            store,
            receipt(
                blockers=["PAPER_PROMOTION_REQUIRED", "PAPER_EXECUTION_NOT_CONFIGURED"], progress=2
            ),
            now=NOW,
        )
        assert report["forecast_status"] == "PROGRESSING"
        assert report["paper_status"] == "OPERATOR_ACTION_REQUIRED"
        assert {a["category"] for a in report["active_alerts"]} == {
            "OPERATOR_ACTION_REQUIRED",
            "MISSING_COMPONENT",
        }
        assert all(a["subsystem"] == "paper_entry" for a in report["active_alerts"])
        assert report["training_status"] == "INSUFFICIENT_EVIDENCE"


def test_wait_after_real_stall_pauses_reminders_without_false_recovery(tmp_path):
    with AlertStore(
        tmp_path / "alerts.db", policy=AlertPolicy(warning_seconds=600, critical_seconds=1200)
    ) as store:
        monitor_progress(store, receipt(), now=NOW)
        monitor_progress(store, receipt(), now=NOW + timedelta(minutes=20))
        paused = monitor_progress(
            store,
            receipt(blockers=["OUTSIDE_FORECAST_SESSION"], expected=False),
            now=NOW + timedelta(hours=8),
        )
        assert paused["forecast_status"] == "EXPECTED_WAIT"
        assert any(a["code"] == "LEARNER_NO_PROGRESS" for a in paused["active_alerts"])
        assert not paused["notifications"]
        resumed = monitor_progress(
            store, receipt(session="2026-10-02"), now=NOW + timedelta(days=1)
        )
        assert resumed["forecast_status"] == "NO_PROGRESS_CRITICAL"
        assert resumed["expected_idle_seconds"] == 1200


def test_processing_error_is_action_required_not_expected_inactivity(tmp_path):
    with AlertStore(tmp_path / "alerts.db") as store:
        failed = receipt(blockers=["OUTCOME_PROCESSING_FAILED"], expected=False)
        failed["status"] = "ERROR"
        result = monitor_progress(store, failed, now=NOW)
        assert result["forecast_status"] == "OPERATOR_ACTION_REQUIRED"
        assert result["active_alerts"][0]["severity"] == "CRITICAL"


def test_failed_measurement_cannot_resolve_preexisting_entry_and_training_gates(tmp_path):
    with AlertStore(tmp_path / "alerts.db") as store:
        before = receipt(blockers=["PAPER_EXECUTION_NOT_CONFIGURED"])
        before["intraday"]["training_status"] = "TRAINING_ATTEMPT_REQUIRES_REVIEW"
        monitor_progress(store, before, now=NOW)
        failed = dict(status="ERROR", blockers=["OUTCOME_PROCESSING_FAILED"])
        after = monitor_progress(store, failed, now=NOW + timedelta(minutes=10))
        assert {a["code"] for a in after["active_alerts"]} == {
            "PAPER_EXECUTION_NOT_CONFIGURED",
            "TRAINING_ATTEMPT_REQUIRES_REVIEW",
            "OUTCOME_PROCESSING_FAILED",
        }
        assert not any(e["event"] == "RECOVERED" for e in after["notifications"])
