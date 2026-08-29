#!/usr/bin/env bash
# Open the multi-node deployment backlog as GitHub issues (#38).
#
# The backlog and its reasoning live in docs/node-deployment.md, which stays authoritative.
# Bodies here are deliberately short and point at it, so the two cannot drift into two
# different plans.
#
# Idempotent: an issue whose exact title already exists is skipped rather than duplicated.
# Re-running after adding a row is safe and is the intended way to use this.
set -euo pipefail

repo="jfhutchi/project_elrond"
doc="docs/node-deployment.md"

command -v gh >/dev/null 2>&1 || {
  printf 'refusing: gh is not installed. See %s for the backlog it would create.\n' "${doc}" >&2
  exit 1
}
gh auth status >/dev/null 2>&1 || {
  printf 'refusing: gh is installed but not authenticated. Run: gh auth login\n' >&2
  exit 1
}

# One API call rather than one per issue, and it includes closed issues so a completed item is
# not reopened as a duplicate.
existing="$(gh issue list --repo "${repo}" --state all --limit 500 --json title --jq '.[].title')"

create() {
  local title="$1" body="$2"
  if printf '%s\n' "${existing}" | grep -Fxq "${title}"; then
    printf 'skip   %s\n' "${title}"
    return 0
  fi
  printf 'create %s\n' "${title}"
  gh issue create --repo "${repo}" --title "${title}" \
    --body "${body}

Part of the multi-node deployment backlog in [\`${doc}\`](../blob/main/${doc}), which carries the
reasoning and the dependency order. Parent: #38. Roadmap: #2.

*Filed by Claude (Opus 5) on behalf of @jfhutchi. Not written by Hutch.*" >/dev/null
}

create "[P1][Node] Reconcile placement.gpu_vram_mb with node capability detection" \
"\`placement._gpu_vram_mb\` returns 0 when \`nvidia-smi\` is absent. That is correct about CUDA and
false about the hardware: on an AMD host it erases the card entirely.

\`quantbot.node.policy.cuda_vram_mb\` now states the same number where its meaning is visible.
Derive placement from it and rename the concept to say CUDA.

Placement behaviour must not change. This was deliberately deferred from the \`quantbot.node\`
commit so that an additive change could not alter a dispatch decision while claiming not to.

Depends on: node identity/capability/policy (delivered)."

create "[P1][Node] Durable node registry with capability snapshots and heartbeat" \
"Registration, capability snapshots and heartbeat freshness, with an Alembic migration.

\`placement.place(job, hosts)\` takes its host list from the caller. Nothing persists a profile,
so there is no inventory behind the capability gate.

A snapshot is a fact about when it was taken. Staleness must block in the same way
\`ENDPOINT_FRESHNESS\` already does, rather than a months-old profile being treated as current."

create "[P1][Node] Node configuration and state-directory layout" \
"\`QUANTBOT_STATE_DIR\` precedence, a per-node config file, and a migration path for the relative
defaults in \`.env.example\`.

\`quantbot.db\`, \`reports\` and \`quantbot.lock\` currently resolve against the working directory,
so durable state follows wherever the process was started. That is workable for one checkout and
wrong for two.

Existing environment variables must keep priority so a working installation is unaffected."

create "[P1][Node] quantbot node CLI: info, register, role" \
"Extends the existing argparse surface in \`cli.py\`. No second binary and no new entry point.

\`node info\` renders measured capabilities and the assigned role. \`node register\` writes to the
registry. \`node role\` reassigns, preserving the node identifier.

Depends on the registry and the configuration layout."

create "[P1][Node] Windows bootstrap: one command from a clean machine" \
"Prerequisites, uv, Python, virtual environment, state directories, configuration, secrets via
the conventions \`scripts/setup-env.ps1\` already uses, and a first \`doctor\`.

Modular steps rather than one opaque script: a failure must name the step that failed.

Must never enable \`LIVE_TRADING\`. \`deploy/omen/setup.sh\` already refuses to deploy against an
env file that sets it, and that check belongs here too."

create "[P1][Node] Service lifecycle and reboot recovery" \
"\`start\`, \`stop\`, \`restart\`, and services that come back after a reboot.

Windows: extend \`scripts/install-daemon-task.ps1\` (Scheduled Task). Linux and WSL: extend the
systemd user units under \`deploy/\`.

Reboot recovery has to be proven end to end, including writer-lock correctness after an
unclean stop."

create "[P1][Node] doctor: node health checks" \
"Services, ledger connectivity, free disk, node registration, heartbeat, credentials present but
never printed, clock sanity, schema version, stale services, configuration drift.

Each check must assert the specific condition it names. A check that passes because something
unrelated succeeded is worse than no check, per CLAUDE.md."

create "[P1][Node] Capability-aware dispatch: route by role and measured capability" \
"#38 recorded that \`place()\` is not wired into the path that starts work, so it is a rule that
exists rather than a rule that runs. This wires it.

Routing keys on the assigned role *and* the measured capability. A node whose \`cuda_vram_mb\` is
0 must never receive CUDA work, and a node given a restricted role must not receive work its
hardware could otherwise carry.

Depends on the registry and the placement reconciliation."

create "[P1][Node] Installer idempotency, repair, upgrade and uninstall" \
"A second run confirms a healthy installation, repairs what is missing, upgrades, or explains
the conflict. It must never destroy a working node.

Uninstall deregisters rather than orphaning a registry row.

Re-running must not repurpose a node: a coordinator quietly demoted to worker would strand the
ledger it holds."

create "[P2][Node] Installer logging and failure diagnosis" \
"A failed installation must say which step failed and where the log is. Structured, and safe to
paste: no credential may reach it."

create "[P2][Node] Retire deploy/omen/ in favour of role-based paths" \
"\`docs/runtime-topology.md\` records \`omen-windows\` as 32 cores and 16,376 MB VRAM. That is the
i9-14900F / RTX 4080 Super desktop, not the HP Omen laptop added in August 2026. Both machines
are HP Omens, so the directory name and the host labels are now ambiguous.

Key deployment paths and host labels on role, not on a product name two machines share."

create "[P2][Node] Multi-node documentation set" \
"Architecture, installation, supported platforms, node roles, hardware guidance, networking and
ports, storage layout, upgrades, repair, uninstall, troubleshooting, \`doctor\`, adding a second
node, and removing or replacing one.

Every port that becomes reachable beyond loopback must be listed with the reason it is open."

create "[P1][Node] Clean-machine acceptance test on the second node" \
"From an unconfigured machine: clone, one bootstrap command, hardware detected, role inferred,
services provisioned, node registered, \`doctor\` green, controller sees the node and its
capabilities, scheduler runs an appropriate workload there, a CUDA-required workload is refused,
reboot recovers the services, a second install proves idempotency, and an upgrade succeeds
without rebuilding the machine.

Automated wherever the step allows it."

printf '\ndone.\n'
