"""Prepare the superseding release locally; never acquire or commission a broker."""
import hashlib
import json
from pathlib import Path

from quantbot.paper_core.release import PreparedRelease, RuntimeConfig, prepare_release, verify_release

root = Path.cwd()
out = root / "docs/evidence/paper-core/m6/fix2"
original = out.parent / "fix1"
config = RuntimeConfig.model_validate_json((original / "prepared-config.json").read_text())
old = PreparedRelease.model_validate_json((original / "prepared-release.json").read_text())
release = prepare_release(config)
(out / "prepared-config.json").write_text(config.model_dump_json(indent=2), encoding="utf-8")
(out / "prepared-release.json").write_text(release.model_dump_json(indent=2), encoding="utf-8")
restored = PreparedRelease.model_validate_json((out / "prepared-release.json").read_text())
restored_config = RuntimeConfig.model_validate_json((out / "prepared-config.json").read_text())
assert restored.id == release.id and restored_config.id == config.id
verification = verify_release(restored)
assert not verification["objections"]
(out / "prepared-verification.json").write_text(json.dumps(verification, indent=2), encoding="utf-8")
request = json.loads((original / "acquisition-request.template.json").read_text())
request["release_id"] = restored.id
(out / "acquisition-request.template.json").write_text(json.dumps(request, indent=2), encoding="utf-8")
supersession = {
    "state": "PREPARED_WAITING_NOT_COMMISSIONED",
    "repair_base": "6026f02fd2ca46c541f9e7c45117f4049ce102b0",
    "old_release_id": old.id,
    "new_release_id": restored.id,
    "config_id": restored_config.id,
    "old_verification_on_repaired_source": verify_release(old),
    "new_verification": verification,
    "config_bytes_unchanged": hashlib.sha256((original / "prepared-config.json").read_bytes()).hexdigest()
        == hashlib.sha256((out / "prepared-config.json").read_bytes()).hexdigest(),
    "executable_pins": len(restored.executable_pins),
    "distributions": len(restored.distributions),
    "operational_release_transfer": "NOT_PERFORMED_UNSUPPORTED_FOR_EXISTING_COMMISSION",
}
assert supersession["old_verification_on_repaired_source"]["objections"]
(out / "supersession.json").write_text(json.dumps(supersession, indent=2), encoding="utf-8")
print(json.dumps(supersession))
