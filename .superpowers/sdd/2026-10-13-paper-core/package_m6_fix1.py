"""Archive nonsecret isolated repair evidence without touching original M6 artifacts."""
import gzip
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

root = Path.cwd()
out = root / "docs/evidence/paper-core/m6/fix1"
prep = root / ".superpowers/sdd/2026-10-13-paper-core"
logs = out / "logs"
exports_dir = out / "exports"
logs.mkdir(exist_ok=True)
exports_dir.mkdir(exist_ok=True)
stages = [
    ("flat-handshake-red", "va9f3hhz"),
    ("one-buy-provider-import-red", "wtqze9ao"),
    ("buy-sell-export-test-setup", "phwcgmr8"),
    ("claim-successor-race-red", "ua0jorfw"),
    ("historical-guardian-time-red", "i0x0frf9"),
    ("flat-handshake-first-green", "rx3r2vv_"),
    ("claim-transition-setup", "tvq162ge"),
    ("claim-terminal-transition-setup", "xuc88kbu"),
    ("claim-successor-green", "_n8jeh8j"),
    ("late-fee-red", "q3c01fr3"),
    ("late-fee-import-setup", "5ravvbpw"),
    ("late-fee-first-green", "treeg8c_"),
    ("post-cost-pre-outcome-green", "3uws8eki"),
    ("claim-send-successor-green", "xam5n3l9"),
    ("precise-date-only-green", "05p9fncr"),
    ("canonical-synthetic-conversion-green", "i_dbme4n"),
    ("frozen-focused-gate", "qzvxoj3o"),
]
records, exports, processes = [], [], []
counts = {}
for stage, token in stages:
    scratch = Path(r"C:\Users\leepa\AppData\Local\Temp") / ("paper-core-check-" + token)
    source = scratch / "result.txt"
    raw = source.read_bytes()
    target = logs / (stage + ".txt.gz")
    target.write_bytes(gzip.compress(raw, mtime=0))
    records.append({
        "stage": stage, "original_log": str(source),
        "archive": target.relative_to(root).as_posix(), "raw_bytes": len(raw),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "compressed_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "result_lines": [line for line in raw.decode("utf-8").splitlines()
            if re.search(r"\d+ (passed|failed|skipped)", line)][-2:],
    })
    if stage == "frozen-focused-gate":
        assert records[-1]["result_lines"] and "failed" not in records[-1]["result_lines"][-1]
        for line in raw.decode("utf-8").splitlines():
            match = re.search(r"(M6_[A-Z0-9_]+):", line)
            if not match:
                continue
            try:
                doc = json.loads(line[match.end():])
            except json.JSONDecodeError:
                continue
            name = match.group(1)
            counts[name] = counts.get(name, 0) + 1
            path = exports_dir / (name.lower() + "-" + str(counts[name]) + ".json")
            path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            exports.append({"marker": name, "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "source_log_raw_sha256": records[-1]["raw_sha256"]})
        for source in sorted((scratch / "pytest").rglob("*")):
            if not source.is_file() or source.name not in {
                "guardian.txt", "outcomes.txt", "trader.txt", "restart-trader.txt",
                "flat-guardian.txt", "flat-outcomes.txt", "flat-trader.txt",
                "process-posts.jsonl", "process-provider-journal.json",
            }:
                continue
            target = logs / ("process-" + source.parent.name + "-" + source.name + ".gz")
            data = source.read_bytes()
            target.write_bytes(gzip.compress(data, mtime=0))
            processes.append({"source": str(source), "path": target.relative_to(root).as_posix(),
                "raw_sha256": hashlib.sha256(data).hexdigest(),
                "compressed_sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
(out / "logs.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
(out / "exports.json").write_text(json.dumps(exports, indent=2), encoding="utf-8")
(out / "process-logs.json").write_text(json.dumps(processes, indent=2), encoding="utf-8")

ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
proposal_records = []
for record in json.loads((out / "task-proposals.json").read_text())["tasks"]:
    path = out / record["path"]
    tree = ET.fromstring(path.read_text(encoding="utf-8"))
    role = record["name"].split()[-1].lower()
    arguments = tree.find("t:Actions/t:Exec/t:Arguments", ns).text
    assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
    assert tree.find("t:RegistrationInfo/t:URI", ns).text == "\\" + record["name"]
    assert tree.find("t:Settings/t:Enabled", ns).text == "false"
    assert tree.find("t:Settings/t:ExecutionTimeLimit", ns).text == "PT0S"
    assert tree.find("t:Principals/t:Principal/t:RunLevel", ns).text == "LeastPrivilege"
    assert not list(tree.find("t:Triggers", ns))
    assert "-SubmissionDisabled" in arguments and "-Role " + role in arguments
    assert str(out) + "\\prepared-config.json" in arguments
    assert str(out) + "\\prepared-release.json" in arguments
    proposal_records.append({"path": path.relative_to(root).as_posix(),
        "sha256": record["sha256"], "verified_static_only": True,
        "role": role, "enabled": False, "triggers": 0,
        "execution_time_limit": "PT0S", "run_level": "LeastPrivilege",
        "submission_disabled": True, "registered_or_started": False})
(out / "task-proposal-verification.json").write_text(json.dumps(proposal_records, indent=2), encoding="utf-8")

files = [
    "tests/integration/paper_core/test_release_recovery.py",
    "tests/integration/paper_core/test_operational_adapter.py",
    "tests/integration/paper_core/test_costed_loop.py",
    "tests/integration/paper_core/test_migration.py",
    "tests/fault/paper_core/test_final_send_authority.py",
    "tests/fault/paper_core/test_protection_authority.py",
    "tests/fault/paper_core/test_outcomes_independent.py",
    "tests/fault/paper_core/test_activation_races.py",
    "tests/fault/paper_core/test_learning_temporal.py",
]
commands = [{"stage": "frozen-focused-gate", "argv": [str(root / ".venv/Scripts/python.exe"),
    str(prep / "check.py"), *files, "-s"], "exit_code": 0,
    "raw_log": records[-1]["original_log"], "raw_log_sha256": records[-1]["raw_sha256"],
    "result": records[-1]["result_lines"][-1]}]
for result in json.loads((out / "ci-results.json").read_text()):
    assert result["exit_code"] == 0
    commands.append({"stage": "six-exact-CI", "argv": result["command"],
        "exit_code": 0, "log": result["log"], "log_sha256": result["sha256"]})
for command in commands:
    command["command_sha256"] = hashlib.sha256(json.dumps(command["argv"], separators=(",", ":")).encode()).hexdigest()
helpers = ["check.py", "ci_m6_fix1.py", "freeze_m6_fix1.py", "prepare_package_m6_fix1.py",
    "task_proposals_m6_fix1.py", "package_m6_fix1.py"]
(out / "commands.json").write_text(json.dumps({
    "environment": "CREDENTIAL_STRIPPED_NETWORK_DISABLED_CHECK_AND_UV_OFFLINE_CI",
    "commands": commands,
    "historical_stages": "Raw logs retained separately; no reconstruction of unrecorded argument lists",
    "helpers": [{"path": (prep / name).relative_to(root).as_posix(),
        "sha256": hashlib.sha256((prep / name).read_bytes()).hexdigest()} for name in helpers],
}, indent=2), encoding="utf-8")
freeze = json.loads((out / "source-pins.json").read_text())
assert all(hashlib.sha256((root / path).read_bytes()).hexdigest() == digest
    for path, digest in freeze["pins"].items())
print(json.dumps({"logs": len(records), "exports": len(exports), "process_logs": len(processes),
    "source_pins_unchanged": len(freeze["pins"]), "final_result": records[-1]["result_lines"]}))
