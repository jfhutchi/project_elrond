"""Verify and index only the repair package; historical indices are never rewritten."""
import gzip
import hashlib
import json
from pathlib import Path
import subprocess

from quantbot.paper_core.release import PreparedRelease, RuntimeConfig, verify_release

root = Path.cwd()
out = root / "docs/evidence/paper-core/m6/fix1"
prep = root / ".superpowers/sdd/2026-10-13-paper-core"
sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
frozen = json.loads((out / "source-pins.json").read_text())
assert all(sha(root / path) == digest for path, digest in frozen["pins"].items())
ci = json.loads((out / "ci-results.json").read_text())
assert len(ci) == 6 and all(r["exit_code"] == 0 and sha(root / r["log"]) == r["sha256"] for r in ci)
for record in json.loads((out / "logs.json").read_text()):
    assert sha(root / record["archive"]) == record["compressed_sha256"]
    raw = gzip.decompress((root / record["archive"]).read_bytes())
    assert hashlib.sha256(raw).hexdigest() == record["raw_sha256"]
for record in json.loads((out / "exports.json").read_text()):
    assert sha(root / record["path"]) == record["sha256"]
    matching = next(r for r in json.loads((out / "logs.json").read_text())
        if r["raw_sha256"] == record["source_log_raw_sha256"])
    raw = gzip.decompress((root / matching["archive"]).read_bytes()).decode("utf-8")
    exported = json.loads((root / record["path"]).read_text())
    assert any(json.loads(line.split(record["marker"] + ":", 1)[1]) == exported
        for line in raw.splitlines() if record["marker"] + ":" in line)
for record in json.loads((out / "process-logs.json").read_text()):
    assert sha(root / record["path"]) == record["compressed_sha256"]
    assert hashlib.sha256(gzip.decompress((root / record["path"]).read_bytes())).hexdigest() == record["raw_sha256"]
old_index = out.parent / "artifacts.json"
old = json.loads(old_index.read_text())
assert all(sha(root / record["path"]) == record["sha256"] for record in old["records"])
old_index_blob = subprocess.check_output(["git", "show", "HEAD:docs/evidence/paper-core/m6/artifacts.json"])
assert hashlib.sha256(old_index_blob).hexdigest() == sha(old_index)
original_report = subprocess.check_output(["git", "show", "HEAD:.superpowers/sdd/2026-10-13-paper-core/m6-report.md"]).decode("utf-8")
report = prep / "m6-report.md"
assert report.read_text(encoding="utf-8").replace("\r\n", "\n").startswith(original_report.replace("\r\n", "\n"))
(out / "original-evidence-preservation.json").write_text(json.dumps({
    "base": "2fd17e18aa860a47bc35c1ba96d4ecc78bcfd202",
    "original_artifact_index_sha256": sha(old_index),
    "original_evidence_records_verified_unchanged": len(old["records"]),
    "report": "ORIGINAL_TEXT_PRESERVED_REPAIR_APPENDED",
    "original_release_executable_verification": "EXPECTED_REFUSAL_ON_REPAIRED_SOURCE",
}, indent=2), encoding="utf-8")
release = PreparedRelease.model_validate_json((out / "prepared-release.json").read_text())
config = RuntimeConfig.model_validate_json((out / "prepared-config.json").read_text())
assert PreparedRelease.model_validate_json(release.model_dump_json()).id == release.id
assert RuntimeConfig.model_validate_json(config.model_dump_json()).id == config.id
assert not verify_release(release)["objections"]
helpers = ["ci_m6_fix1.py", "freeze_m6_fix1.py", "prepare_package_m6_fix1.py",
    "task_proposals_m6_fix1.py", "package_m6_fix1.py", "finalize_m6_fix1.py"]
records = [{"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size, "sha256": sha(path)}
    for path in sorted(out.rglob("*")) if path.is_file() and path.name != "artifacts.json"]
index = {
    "state": "PREPARED_WAITING", "repair_base": "2fd17e18aa860a47bc35c1ba96d4ecc78bcfd202",
    "operational_commission": "NOT_PERFORMED", "broker_state": "NOT_PROBED",
    "release_id": release.id, "config_id": config.id,
    "source_pin_count": len(frozen["pins"]), "focused_result": "115 passed in 173.46s",
    "six_exact_ci_passed": True,
    "report": {"path": report.relative_to(root).as_posix(), "sha256": sha(report)},
    "helpers": [{"path": (prep / name).relative_to(root).as_posix(), "sha256": sha(prep / name)} for name in helpers],
    "records": records,
}
(out / "artifacts.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
print(json.dumps({"records": len(records), "release_id": release.id, "config_id": config.id,
    "original_records_unchanged": len(old["records"]), "index_sha256": sha(out / "artifacts.json")}))
