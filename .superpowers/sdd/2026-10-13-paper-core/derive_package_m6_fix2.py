"""Derive local packaging checks, preserving previous evidence without modification."""
from pathlib import Path

prep = Path.cwd() / ".superpowers/sdd/2026-10-13-paper-core"
package = (prep / "package_m6_fix1.py").read_text(encoding="utf-8")
start = package.index("stages = [")
end = package.index("records, exports, processes", start)
package = package[:start] + '''stages = [
    ("selector-setup", "iw48ezkx"),
    ("unchanged-outcome-setup", "kfco3n1e"),
    ("delayed-zero-admission-red", "rzt4pz8x"),
    ("learning-feature-contract-setup", "h3m7vc0j"),
    ("focused-green", "k9c20_ln"),
    ("frozen-focused-gate", "m8vb5zhu"),
]
''' + package[end:]
package = package.replace("m6/fix1", "m6/fix2").replace("_fix1.py", "_fix2.py")
for line in [
    '    "tests/integration/paper_core/test_migration.py",\n',
    '    "tests/fault/paper_core/test_final_send_authority.py",\n',
    '    "tests/fault/paper_core/test_protection_authority.py",\n',
    '    "tests/fault/paper_core/test_activation_races.py",\n',
]:
    package = package.replace(line, "")
package = package.replace('    "tests/fault/paper_core/test_outcomes_independent.py",\n', "")
package = package.replace('    "tests/fault/paper_core/test_learning_temporal.py",\n', "")
package = package.replace('    "tests/integration/paper_core/test_release_recovery.py",\n', "")
package = package.replace('    "tests/integration/paper_core/test_costed_loop.py",\n', '''    "tests/integration/paper_core/test_costed_loop.py",
    "tests/fault/paper_core/test_outcomes_independent.py",
    "tests/fault/paper_core/test_learning_temporal.py",
    "tests/integration/paper_core/test_release_recovery.py",
''')
(prep / "package_m6_fix2.py").write_text(package, encoding="utf-8")
finalize = (prep / "finalize_m6_fix1.py").read_text(encoding="utf-8")
finalize = finalize.replace("m6/fix1", "m6/fix2").replace("_fix1.py", "_fix2.py")
finalize = finalize.replace("2fd17e18aa860a47bc35c1ba96d4ecc78bcfd202", "6026f02fd2ca46c541f9e7c45117f4049ce102b0")
finalize = finalize.replace('"focused_result": "115 passed in 173.46s"',
    '"focused_result": json.loads((out / "logs.json").read_text())[-1]["result_lines"][-1]')
finalize = finalize.replace('old_index_blob = subprocess.check_output', '''prior_fix1_index = out.parent / "fix1/artifacts.json"
prior_fix1 = json.loads(prior_fix1_index.read_text())
assert all(sha(root / record["path"]) == record["sha256"] for record in prior_fix1["records"])
assert hashlib.sha256(subprocess.check_output(["git", "show", "HEAD:docs/evidence/paper-core/m6/fix1/artifacts.json"])).hexdigest() == sha(prior_fix1_index)
old_index_blob = subprocess.check_output''')
finalize = finalize.replace('"original_evidence_records_verified_unchanged": len(old["records"]),',
    '"original_evidence_records_verified_unchanged": len(old["records"]),\n    "fix1_evidence_records_verified_unchanged": len(prior_fix1["records"]),\n    "fix1_artifact_index_sha256": sha(prior_fix1_index),')
finalize = finalize.replace('    "task_proposals_m6_fix2.py", "package_m6_fix2.py", "finalize_m6_fix2.py"]',
    '    "task_proposals_m6_fix2.py", "package_m6_fix2.py", "finalize_m6_fix2.py",\n    "setup_m6_fix2.py", "derive_package_m6_fix2.py"]')
(prep / "finalize_m6_fix2.py").write_text(finalize, encoding="utf-8")
print("Derived fix2-only package and finalizer")
