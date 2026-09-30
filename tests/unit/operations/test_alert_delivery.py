import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from quantbot.operations.alert_delivery import accept_comment, encode_event, parse_comment

NOW = datetime(2026, 10, 1, 14, tzinfo=UTC)


def event(**changes):
    return dict(
        code="LEARNER_NO_PROGRESS",
        event="OPENED",
        severity="CRITICAL",
        first_seen=NOW.isoformat(),
        **changes,
    )


def test_runtime_free_text_is_never_transmitted_and_wire_roundtrips():
    raw = event(
        action="SECRET account 123 holdings AAPL",
        owner="PRIVATE",
        subsystem="TOKEN",
        category="LOG",
        account_value=1234,
        holdings=["AAPL"],
        incident_id="private-id",
        detail="credential",
    )
    identifier = uuid4()
    wire = encode_event(raw, delivery_id=identifier, emitted_at=NOW)
    parsed = parse_comment(wire)
    assert parsed["delivery_id"] == str(identifier)
    assert parsed["event"] == "NEW"
    assert parsed["category"] == "UNEXPLAINED_STALL"
    assert parsed["subsystem"] == "forecast"
    for forbidden in [
        "SECRET",
        "1234",
        "AAPL",
        "PRIVATE",
        "TOKEN",
        "LOG",
        "private-id",
        "credential",
        "account_value",
        "holdings",
        "owner",
        "incident_id",
    ]:
        assert forbidden not in wire


@pytest.mark.parametrize(
    "field,value",
    [
        ("code", "secret-account"),
        ("code", "LEARNER_NO_PROGRESS\nsecret"),
        ("severity", "CRITICAL secret"),
        ("event", "raw exception"),
        ("first_seen", "2026-10-01T14:00:00+00:00 secret"),
        ("first_seen", "2026-10-01T14:00:00"),
        ("first_seen", 123),
    ],
)
def test_unknown_or_invalid_input_refused_without_echo(field, value):
    raw = event()
    raw[field] = value
    with pytest.raises(ValueError, match="^ALERT_ENVELOPE_REFUSED$"):
        encode_event(raw, delivery_id=uuid4(), emitted_at=NOW)


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("OPENED", "NEW"),
        ("SEVERITY_CHANGED", "NEW"),
        ("REMINDER", "REMINDER"),
        ("RECOVERED", "RECOVERED"),
        ("ACKNOWLEDGED", "ACKNOWLEDGED"),
    ],
)
def test_lifecycle_and_retry_identity(kind, expected):
    raw = event()
    raw["event"] = kind
    identifier = uuid4()
    wire = encode_event(raw, delivery_id=identifier, emitted_at=NOW)
    assert parse_comment(wire)["event"] == expected
    assert encode_event(raw, delivery_id=identifier, emitted_at=NOW) == wire


@pytest.mark.parametrize(
    "change",
    [
        {"holdings": ["AAPL"]},
        {"action": "send credentials"},
        {"subsystem": "account-id"},
        {"category": "private log"},
        {"schema": 2},
        {"delivery_id": "runtime-incident-id"},
        {"severity": "secret"},
        {"emitted_at": NOW.isoformat() + " token"},
    ],
)
def test_chat_parser_rejects_extra_or_tampered_fields(change):
    wire = encode_event(event(), delivery_id=uuid4(), emitted_at=NOW)
    prefix, payload = wire.split("\n", 1)
    doc = json.loads(payload)
    doc.update(change)
    with pytest.raises(ValueError, match="^ALERT_ENVELOPE_REFUSED$"):
        parse_comment(prefix + "\n" + json.dumps(doc))


def test_parser_rejects_duplicate_json_keys_and_development_comments():
    wire = encode_event(event(), delivery_id=uuid4(), emitted_at=NOW)
    for invalid in [
        "Development checkpoint",
        wire.replace('"schema":1', '"schema":1,"schema":1'),
        wire + "\nextra",
        wire.replace("ELROND_ALERT_V1", "ELROND_ALERT_V2"),
    ]:
        with pytest.raises(ValueError, match="^ALERT_ENVELOPE_REFUSED$"):
            parse_comment(invalid)


def test_timestamp_order_and_random_uuid_requirement():
    for identifier, now in [
        (uuid4(), NOW.replace(tzinfo=None)),
        (uuid4(), NOW.replace(year=2025)),
        ("account-id", NOW),
    ]:
        with pytest.raises(ValueError, match="^ALERT_ENVELOPE_REFUSED$"):
            encode_event(event(), delivery_id=identifier, emitted_at=now)


@pytest.mark.parametrize(
    "change",
    [
        {"repository": "another/project_elrond"},
        {"pull_request": 254},
        {"pull_request": True},
        {"author": "untrusted-user"},
        {"author_type": "Bot"},
        {"repository_private": False},
    ],
)
def test_consumer_rejects_wrong_authenticated_origin(change):
    origin = dict(
        repository="jfhutchi/project_elrond",
        pull_request=255,
        author="jfhutchi",
        author_type="User",
        repository_private=True,
    )
    origin.update(change)
    wire = encode_event(event(), delivery_id=uuid4(), emitted_at=NOW)
    with pytest.raises(ValueError, match="^ALERT_ENVELOPE_REFUSED$"):
        accept_comment(wire, **origin)


def test_consumer_accepts_only_pinned_private_human_comment():
    wire = encode_event(event(), delivery_id=uuid4(), emitted_at=NOW)
    assert accept_comment(
        wire,
        repository="jfhutchi/project_elrond",
        pull_request=255,
        author="jfhutchi",
        author_type="User",
        repository_private=True,
    ) == parse_comment(wire)


def test_deep_json_and_noncanonical_timestamp_refused_safely():
    wire = encode_event(event(), delivery_id=uuid4(), emitted_at=NOW)
    prefix, payload = wire.split("\n", 1)
    document = json.loads(payload)
    document["first_seen"] = document["first_seen"].replace("T", "X")
    for invalid in [
        prefix + "\n" + json.dumps(document),
        prefix + "\n" + "[" * 1100 + "0" + "]" * 1100,
    ]:
        with pytest.raises(ValueError, match="^ALERT_ENVELOPE_REFUSED$"):
            parse_comment(invalid)


def test_timestamp_overflow_refused_by_encoder_and_parser():
    raw = event()
    raw["first_seen"] = "0001-01-01T00:00:00+01:00"
    with pytest.raises(ValueError, match="^ALERT_ENVELOPE_REFUSED$"):
        encode_event(raw, delivery_id=uuid4(), emitted_at=NOW)
    prefix, payload = encode_event(event(), delivery_id=uuid4(), emitted_at=NOW).split("\n", 1)
    document = json.loads(payload)
    document["first_seen"] = raw["first_seen"]
    with pytest.raises(ValueError, match="^ALERT_ENVELOPE_REFUSED$"):
        parse_comment(prefix + "\n" + json.dumps(document))


def test_synthetic_canary_has_exact_contract_and_cannot_claim_runtime_health():
    raw = dict(code="DELIVERY_CANARY", event="CANARY", severity="INFO", first_seen=NOW.isoformat())
    parsed = parse_comment(encode_event(raw, delivery_id=uuid4(), emitted_at=NOW))
    assert (parsed["event"], parsed["subsystem"], parsed["category"], parsed["action"]) == (
        "CANARY",
        "delivery",
        "SYNTHETIC_TEST",
        "Confirm this synthetic alert arrived once in the approved Elrond chat.",
    )
    for changes in [{"severity": "CRITICAL"}, {"event": "OPENED"}, {"code": "LEARNER_NO_PROGRESS"}]:
        with pytest.raises(ValueError, match="^ALERT_ENVELOPE_REFUSED$"):
            encode_event(raw | changes, delivery_id=uuid4(), emitted_at=NOW)
