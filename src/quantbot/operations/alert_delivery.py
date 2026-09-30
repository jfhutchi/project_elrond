"""Strict opt-in wire format; no sender, credentials or delivery state changes.

Source alert documents stay private. Only selected enums/timestamps and reviewed
static instructions enter the wire format. A dispatcher must persist a random UUID4
and reuse the same envelope on retry; encoding is not acknowledgement or delivery.
The consumer must separately authenticate repository, PR and comment sender.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

PREFIX = "ELROND_ALERT_V1\n"
FIELDS = {
    "schema",
    "delivery_id",
    "event",
    "code",
    "severity",
    "subsystem",
    "category",
    "first_seen",
    "emitted_at",
    "action",
}
EVENTS = {
    "CANARY": "CANARY",
    "OPENED": "NEW",
    "SEVERITY_CHANGED": "NEW",
    "REMINDER": "REMINDER",
    "RECOVERED": "RECOVERED",
    "ACKNOWLEDGED": "ACKNOWLEDGED",
}
CATALOG = {
    "CLOCK_SYNC_REQUIRED": (
        "clock", "OPERATOR_ACTION_REQUIRED",
        "Inspect Windows Time status and the configured time source. Request explicit "
        "operator approval for a one-time clock resynchronization; honor canceled elevation. "
        "Do not change entry clock tolerances, time servers, services, security or task settings.",
    ),
    "DELIVERY_CANARY": (
        "delivery",
        "SYNTHETIC_TEST",
        "Confirm this synthetic alert arrived once in the approved Elrond chat.",
    ),
    "PAPER_PROMOTION_REQUIRED": (
        "paper_entry",
        "OPERATOR_ACTION_REQUIRED",
        "Paper trading setup incomplete. Engineering must verify the frozen strategy "
        "deployment, clock health and risk controls. Check recorded operator consent before "
        "requesting any missing authorization. This is not a ChatGPT model selection.",
    ),
    "PAPER_EXECUTION_NOT_CONFIGURED": (
        "paper_entry",
        "MISSING_COMPONENT",
        "Paper trading setup incomplete. Engineering must configure and verify the PAPER "
        "adapter and protection handoff using recorded deployment consent. Local forecasts "
        "may continue; configuration does not grant trading authority.",
    ),
    "FORWARD_PRODUCER_MISSING": (
        "forecast",
        "MISSING_COMPONENT",
        "Implement and test a guarded forward producer; request deployment approval.",
    ),
    "OUTCOME_PROCESSING_FAILED": (
        "learner",
        "OPERATOR_ACTION_REQUIRED",
        "Inspect the safe worker failure code and reproduce on an isolated store. "
        "Preserve protected windows and risk controls.",
    ),
    "TRAINING_ATTEMPT_REQUIRES_REVIEW": (
        "training",
        "OPERATOR_ACTION_REQUIRED",
        "Reconcile charged trials with immutable fit receipts before resuming training. "
        "Preserve failed attempts and cumulative search burden.",
    ),
    "LEARNER_NO_PROGRESS": (
        "forecast",
        "UNEXPLAINED_STALL",
        "Inspect data freshness, worker receipts, pending targets and scheduler health. "
        "Preserve protected dates and risk controls.",
    ),
}
for _component in ("LEARNER", "PROTECTION", "RECONCILIATION", "INGESTION"):
    CATALOG[_component + "_HEARTBEAT_STALE"] = (
        _component.lower(),
        "UNVERIFIED_COMPONENT_HEALTH",
        "Inspect component receipts and scheduler health; current health is unverified. "
        "Preserve PAPER limits, kill switch and protection controls.",
    )


def _refuse() -> ValueError:
    return ValueError("ALERT_ENVELOPE_REFUSED")


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise _refuse()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _refuse() from None
    if parsed.tzinfo is None:
        raise _refuse()
    return parsed.astimezone(UTC)


def _validate(document: dict[str, Any]) -> None:
    if set(document) != FIELDS or type(document["schema"]) is not int or document["schema"] != 1:
        raise _refuse()
    code = document["code"]
    if not isinstance(code, str) or code not in CATALOG:
        raise _refuse()
    subsystem, category, action = CATALOG[code]
    if (code == "DELIVERY_CANARY") != (document["event"] == "CANARY"):
        raise _refuse()
    if code == "DELIVERY_CANARY" and document["severity"] != "INFO":
        raise _refuse()
    if (
        document["subsystem"] != subsystem
        or document["category"] != category
        or document["action"] != action
        or document["severity"] not in ("INFO", "WARNING", "CRITICAL")
        or document["event"] not in tuple(EVENTS.values())
    ):
        raise _refuse()
    identifier = document["delivery_id"]
    if not isinstance(identifier, str):
        raise _refuse()
    try:
        parsed = UUID(identifier)
    except ValueError:
        raise _refuse() from None
    if parsed.version != 4 or str(parsed) != identifier:
        raise _refuse()
    first_seen = _timestamp(document["first_seen"])
    emitted_at = _timestamp(document["emitted_at"])
    if (
        document["first_seen"] != first_seen.isoformat()
        or document["emitted_at"] != emitted_at.isoformat()
        or first_seen > emitted_at
    ):
        raise _refuse()


def encode_event(source: dict[str, Any], *, delivery_id: UUID, emitted_at: datetime) -> str:
    """Select safe fields from private input; refuse unknown codes without echoing them."""
    try:
        code = source["code"]
        if not isinstance(code, str) or code not in CATALOG or emitted_at.tzinfo is None:
            raise _refuse()
        subsystem, category, action = CATALOG[code]
        document = dict(
            schema=1,
            delivery_id=str(delivery_id),
            event=EVENTS[source["event"]],
            code=code,
            severity=source["severity"],
            subsystem=subsystem,
            category=category,
            first_seen=_timestamp(source["first_seen"]).isoformat(),
            emitted_at=emitted_at.astimezone(UTC).isoformat(),
            action=action,
        )
        _validate(document)
        return PREFIX + json.dumps(document, sort_keys=True, separators=(",", ":"))
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, RecursionError):
        raise _refuse() from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise _refuse()
        document[key] = value
    return document


def parse_comment(comment: str) -> dict[str, Any]:
    """Fail closed on malformed/tampered payloads; authentication is a separate gate."""
    try:
        if not comment.startswith(PREFIX) or len(comment) > 4096:
            raise _refuse()
        document = json.loads(comment[len(PREFIX) :], object_pairs_hook=_unique_object)
        if not isinstance(document, dict):
            raise _refuse()
        _validate(document)
        return document
    except (TypeError, ValueError, AttributeError, OverflowError, RecursionError):
        raise _refuse() from None


def accept_comment(
    comment: str,
    *,
    repository: str,
    pull_request: int,
    author: str,
    author_type: str,
    repository_private: bool,
) -> dict[str, Any]:
    """Filter metadata fetched through the authorized GitHub connector.

    These values must come from the fetched repository/comment, never from the
    comment body or webhook hints. Pins describe the proposed route; this function
    neither installs it nor authenticates caller-supplied metadata by itself.
    """
    if (
        repository != "jfhutchi/project_elrond"
        or type(pull_request) is not int
        or pull_request != 255
        or author != "jfhutchi"
        or author_type != "User"
        or repository_private is not True
    ):
        raise _refuse()
    return parse_comment(comment)
