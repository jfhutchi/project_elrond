"""Bounded retries retain the exact operator request and durable fence boundary."""

import hashlib
import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_coordinator(monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    path = scripts / "checked_resume.py"
    assert path.exists(), "The checked coordinator must be versioned and import safe"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("checked_resume", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup_request(tmp_path, module, monkeypatch, *, seconds=120):
    clock = SimpleNamespace(value=0.0, wall_offset=0.0)
    start = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return start + timedelta(seconds=clock.value + clock.wall_offset)

    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock.value,
            sleep=lambda seconds: setattr(clock, "value", clock.value + seconds),
        ),
    )
    store = tmp_path / "store.sqlite"
    control = tmp_path / "control.sqlite"
    with sqlite3.connect(store) as db:
        db.executescript(
            "CREATE TABLE schema_version(store_identity TEXT);"
            "INSERT INTO schema_version VALUES('store');"
            "CREATE TABLE kill_switch_state(engaged INTEGER);"
            "INSERT INTO kill_switch_state VALUES(0);"
            "CREATE TABLE core_runtime_events(id TEXT, payload TEXT);"
        )
        db.execute(
            "INSERT INTO core_runtime_events VALUES (?,?)",
            ("fence", json.dumps(dict(sequence=1, action="FENCE_ENTRY"))),
        )
    with sqlite3.connect(control) as db:
        db.executescript(
            "CREATE TABLE entry_bindings(scope TEXT, generation INTEGER,"
            "store_identity TEXT, release_id TEXT);"
            "INSERT INTO entry_bindings VALUES('paper',31,'store','release');"
        )
    manifest = tmp_path / "runtime-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "config": {
                    "repository": str(tmp_path),
                    "core": {"evidence_lane": "BROKER_PAPER"},
                    "store_path": str(store),
                    "request_control": {
                        "entry_generation": 31,
                        "control_path": str(control),
                        "account_scope": "paper",
                    },
                }
            }
        )
    )
    helper = tmp_path / "operations" / "resume_document.py"
    helper.parent.mkdir()
    helper.write_text("# preparation fixture\n")
    (tmp_path / "resume-approval.json").write_text(
        json.dumps({"runtime_release_id": "release", "expected_fence_id": "fence"})
    )
    request = tmp_path / "resume-request.json"
    request.write_text(
        json.dumps(
            {
                "manifest": str(manifest),
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "preparation_helper": str(helper),
                "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
                "generation": 31,
                "store_identity": "store",
                "release_id": "release",
                "fence_id": "fence",
                "approval_reference": "operator",
                "not_before": start.isoformat(),
                "expires_at": (start + timedelta(seconds=seconds)).isoformat(),
            }
        )
    )

    def event(action="RESUME_ENTRY", identity="resumed"):
        with sqlite3.connect(store) as db:
            db.execute(
                "INSERT INTO core_runtime_events VALUES (?,?)",
                (
                    identity,
                    json.dumps(
                        {
                            "sequence": 2,
                            "action": action,
                            "reason": "operator",
                            "evidence_ids": ["fence"],
                        }
                    ),
                ),
            )

    def invoke():
        module.main(["--runtime-root", str(tmp_path), "--request", str(request)])
        return json.loads((tmp_path / "checked-resume-result.json").read_text())

    return SimpleNamespace(clock=clock, store=store, request=request, event=event, invoke=invoke)


@pytest.mark.parametrize("change", [None, "manual_stop", "fence", "request", "durable_resume"])
def test_timeout_rechecks_guards_before_retry(tmp_path, monkeypatch, change):
    module = load_coordinator(monkeypatch)
    case = setup_request(tmp_path, module, monkeypatch)
    calls = []

    def run(command, cwd, env, log, *, timeout, capture):
        assert timeout == 45
        phase = command[command.index("--command") + 1] if "--command" in command else "prepare"
        calls.append(phase)
        timeout_call = 2 if change == "durable_resume" else 1
        if len(calls) == timeout_call:
            if change == "manual_stop":
                with sqlite3.connect(case.store) as db:
                    db.execute("UPDATE kill_switch_state SET engaged=1")
            elif change == "fence":
                case.event("FENCE_ENTRY", "new-fence")
            elif change == "request":
                case.request.write_text(case.request.read_text() + "\n")
            elif change == "durable_resume":
                case.event()
            raise module.CIProcessTimeout("COMMAND_TIMEOUT")
        capture.write('{"objections":[]}')
        if phase == "resume":
            case.event()

    monkeypatch.setattr(module, "run_command", run)
    result = case.invoke()
    if change in {"manual_stop", "fence", "request"}:
        assert result["state"] == "CANCELLED_OR_REFUSED"
        assert calls == ["prepare"]
    else:
        assert result["state"] == "RESUMED_THROUGH_EXISTING_CHECKS"
        assert calls == (["prepare", "resume"] if change else ["prepare", "prepare", "resume"])


def test_expiration_during_prepare_clips_timeout_and_prevents_dispatch(tmp_path, monkeypatch):
    module = load_coordinator(monkeypatch)
    case = setup_request(tmp_path, module, monkeypatch, seconds=12)
    calls = []

    def run(command, cwd, env, log, *, timeout, capture):
        calls.append(command)
        assert timeout == 12
        case.clock.value = 12
        capture.write('{"objections":[]}')

    monkeypatch.setattr(module, "run_command", run)
    result = case.invoke()
    assert result["state"] == "EXPIRED_STILL_FENCED"
    assert len(calls) == 1


def test_verification_failure_is_terminal(tmp_path, monkeypatch):
    module = load_coordinator(monkeypatch)
    case = setup_request(tmp_path, module, monkeypatch)
    calls = []

    def run(command, cwd, env, log, *, timeout, capture):
        calls.append(command)
        if "--command" in command:
            capture.write("ValueError: ['EXECUTABLE_CHANGED']\n")
            raise module.CIProcessError("COMMAND_FAILED: executable exit 1")

    monkeypatch.setattr(module, "run_command", run)
    result = case.invoke()
    assert result["state"] == "CANCELLED_OR_REFUSED"
    assert len(calls) == 2


def test_backward_clock_cannot_extend_remaining_child_budget(tmp_path, monkeypatch):
    module = load_coordinator(monkeypatch)
    case = setup_request(tmp_path, module, monkeypatch, seconds=12)
    budgets = []

    def run(command, cwd, env, log, *, timeout, capture):
        budgets.append(timeout)
        if len(budgets) == 1:
            case.clock.value = 7
            case.clock.wall_offset = -100
        else:
            case.event()

    monkeypatch.setattr(module, "run_command", run)
    assert case.invoke()["state"] == "RESUMED_THROUGH_EXISTING_CHECKS"
    assert budgets == [12, 5]


def test_freshness_refusal_regenerates_evidence_before_retry(tmp_path, monkeypatch):
    module = load_coordinator(monkeypatch)
    case = setup_request(tmp_path, module, monkeypatch)
    calls = []

    def run(command, cwd, env, log, *, timeout, capture):
        phase = "resume" if "--command" in command else "prepare"
        calls.append(phase)
        if len(calls) == 2:
            capture.write("quantbot.storage.StateConflictError: RESUME_FRESH_TIME_REQUIRED\n")
            raise module.CIProcessError("COMMAND_FAILED: executable exit 1")
        if len(calls) == 4:
            case.event()

    monkeypatch.setattr(module, "run_command", run)
    assert case.invoke()["state"] == "RESUMED_THROUGH_EXISTING_CHECKS"
    assert calls == ["prepare", "resume", "prepare", "resume"]
