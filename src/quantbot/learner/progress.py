"""Measured progress and actionable gate classification; no promotion authority."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from quantbot.operations.alerts import AlertStore, Condition, Severity

GATES = {
    "PAPER_PROMOTION_REQUIRED": Condition(
        "PAPER_PROMOTION_REQUIRED",
        "paper_entry",
        "CRITICAL",
        "OPERATOR_ACTION_REQUIRED",
        "Engineering / Operator",
        "Prepare an empirically eligible, frozen evidence package for the exact PAPER model "
        "deployment; then request operator approval. This gate does not prevent local forecasts. "
        "Approval cannot replace empirical evidence or establish profitability.",
    ),
    "PAPER_EXECUTION_NOT_CONFIGURED": Condition(
        "PAPER_EXECUTION_NOT_CONFIGURED",
        "paper_entry",
        "CRITICAL",
        "MISSING_COMPONENT",
        "Engineering",
        "Prepare and test the PAPER execution adapter and protection handoff; "
        "request explicit deployment approval. Local forecasts may continue.",
    ),
    "FORWARD_PRODUCER_MISSING": Condition(
        "FORWARD_PRODUCER_MISSING",
        "forecast",
        "CRITICAL",
        "MISSING_COMPONENT",
        "Engineering",
        "Implement and test a guarded forward producer; approve its deployment.",
    ),
    "OUTCOME_PROCESSING_FAILED": Condition(
        "OUTCOME_PROCESSING_FAILED",
        "learner",
        "CRITICAL",
        "OPERATOR_ACTION_REQUIRED",
        "Engineering",
        "Inspect the safe worker failure code and reproduce on an isolated store. "
        "Do not clear protected windows or risk controls.",
    ),
    "TRAINING_ATTEMPT_REQUIRES_REVIEW": Condition(
        "TRAINING_ATTEMPT_REQUIRES_REVIEW",
        "training",
        "CRITICAL",
        "OPERATOR_ACTION_REQUIRED",
        "Engineering",
        "Reconcile charged trial IDs with immutable fit receipts. Preserve "
        "failed attempts and cumulative search burden before resuming training.",
    ),
}
WAITS = {"OUTSIDE_FORECAST_SESSION", "PROTECTED_FEATURE_WINDOW", "FORECAST_LOOKBACK_WARMUP"}


def monitor_progress(
    store: AlertStore, receipt: dict[str, Any], *, now: datetime
) -> dict[str, Any]:
    previous = store.state("learner_progress")
    if previous and now < datetime.fromisoformat(previous["at"]):
        raise ValueError("progress clock moved backwards")
    intraday = receipt.get("intraday", {})
    codes = set(receipt.get("blockers", ()))
    training = intraday.get("training_status", "TRAINING_DATA_WARMUP")
    if training in GATES:
        codes.add(training)
    if receipt.get("status") == "ERROR":
        # An incomplete measurement cannot demonstrate that a prior gate cleared.
        # Refresh known unresolved conditions so failures do not suppress reminders either.
        codes.update(a["code"] for a in store.active() if a["code"] in GATES)
    conditions = [GATES[code] for code in sorted(codes & GATES.keys())]
    productive = any(
        intraday.get(key, 0) > 0
        for key in (
            "observations_this_run",
            "predictions_this_run",
            "outcomes_this_run",
            "trained_this_run",
        )
    )
    productive |= any(
        receipt.get(key, 0) > 0 for key in ("resolved_this_run", "evaluated_this_run")
    )
    expected = intraday.get("progress_expected") is True and not bool(codes & WAITS)
    session = intraday.get("progress_session")
    seconds = previous.get("expected_idle_seconds", 0)
    # Accumulate only intervals with two eligible checks in the same measured session.
    # Closed/protected/warmup periods and unknown startup gaps add no fabricated stall time.
    if (
        expected
        and previous.get("expected")
        and session == previous.get("session")
        and previous.get("at")
    ):
        seconds += (now - datetime.fromisoformat(previous["at"])).total_seconds()
    if productive:
        seconds = 0
    last = now.isoformat() if productive else previous.get("last_progress_at")
    status = "PROGRESSING" if productive else ("AWAITING_PROGRESS" if expected else "EXPECTED_WAIT")
    retained: tuple[str, ...] = ()
    existing = next((a for a in store.active() if a["code"] == "LEARNER_NO_PROGRESS"), None)
    if not expected and not productive and existing:
        retained = ("LEARNER_NO_PROGRESS",)
    if expected and not productive:
        if seconds >= store.policy.warning_seconds or existing:
            severity: Severity = (
                "CRITICAL"
                if seconds >= store.policy.critical_seconds
                or (existing and existing["severity"] == "CRITICAL")
                else "WARNING"
            )
            status = "NO_PROGRESS_" + severity
            conditions.append(
                Condition(
                    "LEARNER_NO_PROGRESS",
                    "forecast",
                    severity,
                    "UNEXPLAINED_STALL",
                    "Engineering",
                    "Inspect minute-data freshness, worker receipts and pending targets. Last "
                    f"productive progress: {last or 'not yet observed'}. Check scheduler/sign-in "
                    "and provider health; preserve the protected-date boundary.",
                )
            )
    if receipt.get("status") == "ERROR":
        status = "OPERATOR_ACTION_REQUIRED"
    elif "FORWARD_PRODUCER_MISSING" in codes:
        status = "MISSING_COMPONENT"
    events = store.update(conditions, now=now, retain_codes=retained)
    store.save_state(
        "learner_progress",
        dict(
            at=now.isoformat(),
            expected=expected,
            session=session,
            expected_idle_seconds=seconds,
            last_progress_at=last,
        ),
    )
    return dict(
        forecast_status=status,
        last_progress_at=last,
        expected_idle_seconds=seconds,
        paper_status=(
            "OPERATOR_ACTION_REQUIRED"
            if "PAPER_PROMOTION_REQUIRED" in codes
            else "MISSING_COMPONENT"
            if "PAPER_EXECUTION_NOT_CONFIGURED" in codes
            else "UNVERIFIED"
        ),
        training_status=(
            "INSUFFICIENT_EVIDENCE" if training == "TRAINING_DATA_WARMUP" else training
        ),
        wait_reasons=sorted(codes & WAITS),
        active_alerts=store.active(),
        notifications=events,
        notification_delivery="LOCAL_STDOUT_AND_CONSOLE; EXTERNAL_NOT_CONFIGURED",
    )
