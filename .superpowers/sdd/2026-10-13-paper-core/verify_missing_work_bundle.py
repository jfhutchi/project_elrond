"""Read-only owner verification of the committed final repair package."""
import gzip
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

bundle = Path(sys.argv[1])
head = sys.argv[2]
sha = lambda data: hashlib.sha256(data).hexdigest()
read = lambda path: json.loads(path.read_text(encoding="utf-8"))
index = read(bundle / "artifact-index.json")
index_path = (bundle / "artifact-index.json").resolve().relative_to(Path.cwd()).as_posix()
assert subprocess.check_output(["git", "show", head + ":" + index_path]) == (bundle / "artifact-index.json").read_bytes()
assert index["source_pins_sha256"] == sha((bundle / "source-pins.json").read_bytes())
for row in index["files"]:
    path = bundle / row["path"]
    raw = path.read_bytes()
    assert len(raw) == row["bytes"] and sha(raw) == row["sha256"], str(path)
    relative = path.resolve().relative_to(Path.cwd()).as_posix()
    committed = subprocess.check_output(["git", "show", head + ":" + relative])
    assert committed == raw, relative
sources = {}
for row in index["export_provenance"] + index["historical_export_provenance"]:
    source = row["source_log"]
    if source not in sources:
        sources[source] = gzip.decompress((bundle / source).read_bytes()).decode("utf-8").splitlines()
    line = sources[source][row["source_line"] - 1]
    payload = line.split(row["marker"] + ":", 1)[1]
    assert sha(payload.encode()) == row["raw_json_sha256"], row
    assert json.loads(payload) == read(bundle / row["path"])[row["occurrence"]], row
    phase = Path(source).parent
    assert sha((bundle / phase / "source-pins.json").read_bytes()) == row["source_pins_sha256"]
for row in index["process_logs"]:
    archive = (bundle / row["path"]).read_bytes()
    raw = gzip.decompress(archive)
    assert sha(archive) == row["compressed_sha256"]
    assert sha(raw) == row["raw_sha256"] and len(raw) == row["raw_bytes"]
    assert raw == Path(row["original"]).read_bytes()
stages = read(bundle / "logs.json")
for row in stages:
    archive = (bundle / row["archive"]).read_bytes()
    raw = gzip.decompress(archive)
    assert sha(archive) == row["compressed_sha256"] and sha(raw) == row["raw_sha256"]
    assert raw == Path(row["original"]).read_bytes()
pins = read(bundle / "source-pins.json")["pins"]
with zipfile.ZipFile(bundle / "frozen-source.zip") as archive:
    assert all(sha(Path(path).read_bytes()) == digest == sha(archive.read(path)) for path, digest in pins.items())
ci = read(bundle / "ci-results.json")
assert len(ci) == 6 and all(row["exit_code"] == 0 and sha(Path(row["log"]).read_bytes()) == row["sha256"] for row in ci)
old = []
for suffix in ["m6", "m6/fix1", "m6/fix2"]:
    prior = read(Path("docs/evidence/paper-core") / suffix / "artifacts.json")
    assert all(sha(Path(row["path"]).read_bytes()) == row["sha256"] for row in prior["records"])
    old.append({"bundle": suffix, "unchanged_records": len(prior["records"])})
prior_final = Path("docs/evidence/paper-core/final-repair")
prior_final_index = read(prior_final / "artifact-index.json")
for record in prior_final_index["files"]:
    file = prior_final / record["path"]
    raw = file.read_bytes()
    assert len(raw) == record["bytes"] and sha(raw) == record["sha256"]
    relative = file.resolve().relative_to(Path.cwd()).as_posix()
    assert subprocess.check_output(["git", "show", "ee079d387148e691f3b11152e218dc11dfcc4a1e:" + relative]) == raw
assert sha((prior_final / "artifact-index.json").read_bytes()) == "b0bd69622d92daf98229af180e66aff6eaf8d2b56b80efbb59db0499280bef93"
old.append({"bundle": "final-repair", "unchanged_records": len(prior_final_index["files"])})
prior_residual = Path("docs/evidence/paper-core/residual-cutoff")
prior_residual_index = read(prior_residual / "artifact-index.json")
for record in prior_residual_index["files"]:
    file = prior_residual / record["path"]
    raw = file.read_bytes()
    assert len(raw) == record["bytes"] and sha(raw) == record["sha256"]
    relative = file.resolve().relative_to(Path.cwd()).as_posix()
    assert subprocess.check_output(["git", "show", "fa7a1fb561ccd113e6d47d017c12bd1d89713426:" + relative]) == raw
assert sha((prior_residual / "artifact-index.json").read_bytes()) == "40dc3ce35409d1126326c12d911bd617f19af2a7e779bb948da9351c9cb3b09f"
old.append({"bundle": "residual-cutoff", "unchanged_records": len(prior_residual_index["files"])})
result = {
    "head": head, "artifact_index_sha256": sha((bundle / "artifact-index.json").read_bytes()),
    "records_working_and_git": len(index["files"]), "stage_logs_raw_compressed_original": len(stages),
    "exports_semantically_joined": len(index["export_provenance"]),
    "historical_exports_semantically_joined": len(index["historical_export_provenance"]),
    "process_archives_raw_compressed_original": len(index["process_logs"]),
    "source_pins_working_and_archive": len(pins), "ci_checks": len(ci),
    "prior_bundles": old,
}
Path(sys.argv[3]).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result))
