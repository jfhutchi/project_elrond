# Mako Windows security update

Dependency fix: `a3d5880f02f9d57104e3e833474a4648d1ec181e`, based on published branch `6298ccec177f248a65cb40ab73788e5995b08955`. The scoped independent review found no actionable issues. This updates the prepared release; it does not establish operational commissioning or natural PAPER acceptance.

## Change and applicability

[GHSA-5639-2j2p-m4mx / CVE-2026-102991](https://github.com/advisories/GHSA-5639-2j2p-m4mx) affects Mako through 1.4.1: drive-designator template URIs can escape a configured template directory on Windows. The first patched version is 1.4.2. [Mako 1.4.3](https://docs.makotemplates.org/en/latest/changelog.html#change-1.4.3) retains the fix and corrects upstream Windows tests.

`pyproject.toml` now requires `mako>=1.4.2,<2`, protecting installations that resolve without the lockfile. `uv.lock` selects 1.4.3 with registry artifact hashes. Mako is the only package whose locked version changes; Alembic remains 1.20.0. The prepared virtual environment has been synchronized with the frozen lockfile.

Elrond brings Mako in through Alembic. A source search found no application use of `TemplateLookup`; the console renders with Jinja2, and Alembic uses `mako.template.Template` with its migration templates. This limits the observed exposure but is not a claim that every possible integration is unexploitable. The vulnerable dependency is removed regardless.

## Verification

- Native Windows reproduction, using temporary sentinel files only: six traversal cases failed to raise on Mako 1.4.1; normal rendering passed. On 1.4.3, all seven cases pass. Both slash styles and three drive-designator variants are covered.
- Relevant offline regression gate: **169 passed, 26 skipped in 203.21 seconds**. This includes the security tests, operational/forecast database tests, migration authority, SQLite storage parity, PAPER migration/release recovery, final-repair controls, and outcome-cutoff recovery. All skips require a disposable PostgreSQL server; none conceal Windows security tests.
- Ruff over `src` and `tests`, frozen lock consistency, and installed dependency compatibility pass. No hosted CI was triggered. This is not a full-repository or PostgreSQL-green claim.
- Tests use the existing credential-stripped, network-blocked check wrapper and disposable database paths. Red/green logs and command/hash metadata are retained in this directory.

## Successor release

- Prepared release: `79e9155a7b54dd542b422cf2b3d11f71bb3616154de122fc7f27bc8f59147791`.
- Unchanged config: `8c95e278bc674c64557bb0485957d1b6666546e1d67a20e43d30d2079b085c8c`; schema 36.
- All 420 source pins match. Only `pyproject.toml` and `uv.lock` differ among the predecessor's executable pins; the security test is added. Application, launcher, interpreter, economic configuration, and learning policy remain unchanged.
- Typed release verification succeeds. The prior `2377ee8f...` release correctly refuses the changed dependency/configuration pins and installed distributions. Historical bundles are preserved, not rewritten to claim patched dependencies.
- Four static task proposals now reference this directory. Functional proposals remain disabled, trigger-free, submission-disabled and entry-disabled.

Use this directory's `prepared-config.json` and `prepared-release.json` for the staged [handover](../handover.md). The existing account, ownership, backup, migration, commissioning, role-readiness and natural PAPER acceptance checks still apply. No default-branch merge or alert dismissal is performed; Dependabot's default-branch alert can remain open while the fix is in PR #289.

## Selected-user deployment preflight

After the user authorized deployment and enabling scripts, the existing HP Omen hidden host ran the exact verifier-only action under `leepa / Interactive / Limited`. The first attempts failed before Python because the native Windows PowerShell task context had no execution policy set. The user's script-enablement request authorized setting **CurrentUser RemoteSigned** in that context; MachinePolicy, UserPolicy, Process, and LocalMachine remain Undefined. No Bypass flag was introduced.

The retry completed with task exit 0 and the exact patched release `PREPARED`, zero objections, `NOT_PROBED` and `NOT_PERFORMED` for broker state and commission. The trigger-free verifier task is disabled again and its original proposed action restored. Logs retain both failed attempts and successful verification. This establishes launcher/policy/log access under the selected account, not functional-role readiness, migration, commission, or trading. Other services were not stopped or reconfigured by this preflight.
