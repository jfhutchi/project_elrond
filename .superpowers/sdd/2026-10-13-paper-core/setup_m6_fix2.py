"""Derive repair-only preparation helpers without altering previous stages."""
import gzip
import hashlib
import json
from pathlib import Path

root = Path.cwd()
prep = root / ".superpowers/sdd/2026-10-13-paper-core"
out = root / "docs/evidence/paper-core/m6/fix2"
for name in ["ci_m6", "freeze_m6", "prepare_package_m6", "task_proposals_m6"]:
    source = (prep / (name + "_fix1.py")).read_text(encoding="utf-8")
    if name == "freeze_m6":
        source = source.replace("m6/fix1/source-pins.json", "m6/fix2/source-pins.json")
        source = source.replace("doc={'frozen_at'", "paths += [root/'.superpowers/sdd/2026-10-13-paper-core/ci_m6_fix2.py']\ndoc={'frozen_at'")
    else:
        source = source.replace("m6/fix1", "m6/fix2")
    if name == "prepare_package_m6":
        source = source.replace("original = out.parent", 'original = out.parent / "fix1"')
        source = source.replace("2fd17e18aa860a47bc35c1ba96d4ecc78bcfd202", "6026f02fd2ca46c541f9e7c45117f4049ce102b0")
    (prep / (name + "_fix2.py")).write_text(source, encoding="utf-8")
source = Path(r"C:\Users\leepa\AppData\Local\Temp\m6-fix1-review-fee-race.py")
log = Path(r"C:\Users\leepa\AppData\Local\Temp\paper-core-check-_n9_xij6\result.txt")
expected_source = "b115291a72a40d1745ae44ddd6572b1750857defa45d95c6dabab9f37ac720bc"
expected_log = "8b983e04b5a312590485b467a0e4d95274ea4e078c29b6ba1906037ad8090899"
assert hashlib.sha256(source.read_bytes()).hexdigest() == expected_source
assert hashlib.sha256(log.read_bytes()).hexdigest() == expected_log
(out / "reviewer-fee-race.py.txt").write_bytes(source.read_bytes())
archive = out / "reviewer-fee-race.txt.gz"
archive.write_bytes(gzip.compress(log.read_bytes(), mtime=0))
(out / "reviewer-provenance.json").write_text(json.dumps({
    "source": str(source), "source_sha256": expected_source,
    "raw_log": str(log), "raw_log_sha256": expected_log,
    "compressed_log_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    "scope": "DIAGNOSTIC_ASSERTS_PRE_FIX_DEFECT_NOT_ACCEPTANCE",
}, indent=2), encoding="utf-8")
print("Derived four fix2-only helpers; reviewer diagnostic bytes verified and archived")
