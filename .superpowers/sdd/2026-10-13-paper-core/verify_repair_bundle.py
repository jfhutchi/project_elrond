"""Read-only owner verification of frozen repair evidence; no tests or operational access."""
import gzip
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

bundle = Path(sys.argv[1])
head = sys.argv[2]
sha = lambda data: hashlib.sha256(data).hexdigest()
read = lambda path: json.loads(path.read_text(encoding="utf-8"))
index = read(bundle / "artifacts.json")
normalized = []
for record in index["records"] + [index["report"]] + index["helpers"]:
    path = Path(record["path"])
    working = path.read_bytes()
    assert sha(working) == record["sha256"], str(path)
    committed = subprocess.check_output(["git", "show", head + ":" + record["path"]])
    if sha(committed) != record["sha256"]:
        assert record not in index["records"], str(path)
        assert working.replace(b"\r\n", b"\n") == committed.replace(b"\r\n", b"\n"), str(path)
        normalized.append({"path": str(path), "working_sha256": sha(working), "git_sha256": sha(committed)})
logs = {}
for record in read(bundle / "logs.json"):
    archive = Path(record["archive"]).read_bytes()
    raw = gzip.decompress(archive)
    assert sha(archive) == record["compressed_sha256"]
    assert sha(raw) == record["raw_sha256"]
    assert raw == Path(record["original_log"]).read_bytes()
    logs[sha(raw)] = raw.decode("utf-8")
processes = read(bundle / "process-logs.json")
for record in processes:
    archive = Path(record["path"]).read_bytes()
    raw = gzip.decompress(archive)
    assert sha(archive) == record["compressed_sha256"]
    assert sha(raw) == record["raw_sha256"]
    assert raw == Path(record["source"]).read_bytes()
seen = defaultdict(int)
for record in read(bundle / "exports.json"):
    marker = record["marker"]
    matches = [json.loads(line.split(marker + ":", 1)[1]) for line in logs[record["source_log_raw_sha256"]].splitlines() if marker + ":" in line]
    assert matches[seen[marker]] == read(Path(record["path"])), marker
    seen[marker] += 1
pins = read(bundle / "source-pins.json")["pins"]
assert all(sha(Path(path).read_bytes()) == digest for path, digest in pins.items())
ci = read(bundle / "ci-results.json")
assert len(ci) == 6
assert all(record["exit_code"] == 0 and sha(Path(record["log"]).read_bytes()) == record["sha256"] for record in ci)
result = {"head": head, "artifact_index_sha256": sha((bundle / "artifacts.json").read_bytes()), "records_working_and_git": len(index["records"]), "logs_raw_compressed_original": len(logs), "process_archives": len(processes), "exports_semantically_joined": sum(seen.values()), "source_pins": len(pins), "ci_checks": len(ci), "auxiliary_line_ending_normalization": normalized}
Path(sys.argv[3]).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result))
