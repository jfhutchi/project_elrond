"""One bounded maintenance-fence resume through the unchanged authoritative CLI."""

import argparse
import hashlib
import io
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from ci_process import CIProcessError, CIProcessTimeout, run_command


class RequestExpired(Exception):
    """The original authorization window has ended."""


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument(
        "--request", type=Path, default=Path(__file__).resolve().with_name("resume-request.json")
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    request_path = args.request.resolve()
    folder = request_path.parent
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request_digest = hashlib.sha256(request_path.read_bytes()).hexdigest()
    manifest = Path(request["manifest"])
    helper = Path(request["preparation_helper"])
    root = args.runtime_root.resolve()
    python = root / ".venv/Scripts/python.exe"
    begin = datetime.fromisoformat(request["not_before"])
    end = datetime.fromisoformat(request["expires_at"])
    assert 0 < (end - begin).total_seconds() <= 1200
    deadline = time.monotonic() + max(0, (end - datetime.now(UTC)).total_seconds())
    env = dict(os.environ, PYTHONPATH=str(root / "src"))
    env.pop("PYTHONHOME", None)

    def remaining():
        return min((end - datetime.now(UTC)).total_seconds(), deadline - time.monotonic())

    def current():
        assert request_path.exists(), "REQUEST_WITHDRAWN"
        assert hashlib.sha256(request_path.read_bytes()).hexdigest() == request_digest, (
            "REQUEST_CHANGED"
        )
        assert hashlib.sha256(manifest.read_bytes()).hexdigest() == request["manifest_sha256"], (
            "RELEASE_CHANGED"
        )
        assert hashlib.sha256(helper.read_bytes()).hexdigest() == request["helper_sha256"], (
            "HELPER_CHANGED"
        )
        config = json.loads(manifest.read_text(encoding="utf-8"))["config"]
        assert Path(config["repository"]).resolve() == root
        assert config["core"]["evidence_lane"] == "BROKER_PAPER"
        assert config["request_control"]["entry_generation"] == request["generation"]
        with closing(
            sqlite3.connect(Path(config["store_path"]).as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            assert (
                db.execute("SELECT store_identity FROM schema_version").fetchone()[0]
                == request["store_identity"]
            )
            assert db.execute("SELECT engaged FROM kill_switch_state").fetchone()[0] == 0, (
                "MANUAL_STOP"
            )
            events = [
                (r[0], json.loads(r[1]))
                for r in db.execute("SELECT id,payload FROM core_runtime_events")
            ]
            identity, event = max(events, key=lambda pair: pair[1]["sequence"])
        if (
            event["action"] == "RESUME_ENTRY"
            and event["reason"] == request["approval_reference"]
            and request["fence_id"] in event["evidence_ids"]
        ):
            return config, identity
        assert identity == request["fence_id"] and event["action"] == "FENCE_ENTRY", "FENCE_CHANGED"
        control = config["request_control"]
        with closing(
            sqlite3.connect(
                Path(control["control_path"]).as_uri() + "?mode=ro", uri=True, timeout=5
            )
        ) as db:
            bound = db.execute(
                "SELECT generation,store_identity,release_id FROM entry_bindings WHERE scope=?",
                (control["account_scope"],),
            ).fetchone()
            assert bound == (
                request["generation"],
                request["store_identity"],
                request["release_id"],
            ), "ACCOUNT_BINDING_CHANGED"
        return config, None

    def run(arguments):
        budget = min(45, remaining())
        if budget <= 0:
            raise RequestExpired()
        capture = io.StringIO()
        try:
            run_command(
                [str(python), "-B", *arguments],
                root,
                env,
                io.StringIO(),
                timeout=budget,
                capture=capture,
            )
        except CIProcessError as error:
            error.output = capture.getvalue()
            raise

    attempts = 0
    last_reason = None
    resumed = None
    try:
        _, resumed = current()
        if args.check:
            assert remaining() > 0, "REQUEST_EXPIRED"
            print(
                json.dumps(
                    dict(
                        state="VALIDATED_NOT_RESUMED",
                        not_before=begin.isoformat(),
                        expires_at=end.isoformat(),
                    )
                )
            )
            return 0
        while not resumed and datetime.now(UTC) < begin:
            _, resumed = current()
            if remaining() <= 0:
                raise RequestExpired()
            time.sleep(min(60, remaining(), max(0, (begin - datetime.now(UTC)).total_seconds())))
        while not resumed and remaining() > 0 and attempts < 60:
            _, resumed = current()
            if resumed:
                break
            attempts += 1
            try:
                # The normal resume CLI verifies the complete pinned executable before
                # reading the approval or mutating authority. Avoid a duplicate scan.
                try:
                    run([str(helper)])
                except CIProcessTimeout:
                    raise
                except CIProcessError as error:
                    if not str(error).startswith("COMMAND_FAILED:"):
                        raise
                    last_reason = "CURRENT_READINESS_NOT_SATISFIED"
                else:
                    approval = json.loads(
                        (helper.parent.parent / "resume-approval.json").read_text(encoding="utf-8")
                    )
                    assert (
                        approval["runtime_release_id"] == request["release_id"]
                        and approval["expected_fence_id"] == request["fence_id"]
                    )
                    approval["approval_reference"] = request["approval_reference"]
                    path = folder / "checked-resume-approval.json"
                    path.write_text(json.dumps(approval, indent=2), encoding="utf-8")
                    _, resumed = current()
                    if resumed:
                        break
                    try:
                        run(
                            [
                                "-m",
                                "quantbot.paper_core",
                                "--config",
                                str(manifest.with_name("runtime-config.json")),
                                "--manifest",
                                str(manifest),
                                "--command",
                                "resume",
                                "--approval-document",
                                str(path),
                            ]
                        )
                        last_reason = None
                    except CIProcessTimeout:
                        raise
                    except CIProcessError as error:
                        # Only known fresh-evidence races are retryable. Executable
                        # verification failures and other unknown refusals remain fatal.
                        final_line = getattr(error, "output", "").strip().splitlines()[-1:]
                        readiness = (
                            "RESUME_FRESH_GUARDIAN_REQUIRED",
                            "RESUME_FRESH_PROOF_REQUIRED",
                            "RESUME_FRESH_TIME_REQUIRED",
                        )
                        if not (
                            str(error).startswith("COMMAND_FAILED:")
                            and final_line
                            and any(
                                final_line[0].endswith("StateConflictError: " + reason)
                                for reason in readiness
                            )
                        ):
                            raise
                        last_reason = final_line[0]
            except CIProcessTimeout as error:
                # run_command has completed process-tree cleanup before this catch.
                last_reason = str(error)
            _, resumed = current()
            if resumed:
                break
            time.sleep(min(15, max(0, remaining())))
        state = "RESUMED_THROUGH_EXISTING_CHECKS" if resumed else "EXPIRED_STILL_FENCED"
    except (
        RequestExpired,
        AssertionError,
        OSError,
        ValueError,
        sqlite3.Error,
        CIProcessError,
    ) as error:
        state = (
            "EXPIRED_STILL_FENCED" if isinstance(error, RequestExpired) else "CANCELLED_OR_REFUSED"
        )
        last_reason = str(error)
        # A command may commit before losing its output. Never dispatch again
        # before observing durable authority, and never clear a fence directly.
        try:
            _, resumed = current()
            if resumed:
                state = "RESUMED_THROUGH_EXISTING_CHECKS"
        except (AssertionError, OSError, ValueError, sqlite3.Error) as current_error:
            state = "CANCELLED_OR_REFUSED"
            last_reason = str(current_error)
    result = dict(
        at=datetime.now(UTC).isoformat(),
        state=state,
        attempts=attempts,
        release_id=request["release_id"],
        last_reason=last_reason,
    )
    if not args.check:
        (folder / "checked-resume-result.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
    print(json.dumps(result), flush=True)
    return 0 if state == "RESUMED_THROUGH_EXISTING_CHECKS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
