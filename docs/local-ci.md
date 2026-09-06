# Local CI on Omen (#218)

From a clean, committed checkout, run:

```powershell
.\scripts\local-ci.ps1
```

This requests Linux and native Windows verification. A missing prerequisite or failed requested
job returns nonzero. Logs stream to the terminal and are retained with `summary.json` under
`.local-ci/<run-id>/`. Each platform reports PASS, FAIL or NOT RUN. The report identifies the
original selected commit even though each exported checkout has a disposable Git baseline. A
NOT RUN status does not claim that commit was tested; failed jobs also retain the selected SHA.

## Setup is an explicit operator step

Required on Windows: Git for Windows, uv on PATH, and a managed Python 3.11 installed with
`uv python install 3.11`. The entry point only locates that interpreter; it does not install tools.
Start from a trusted checkout. No account tokens are needed.

For Linux verification on Windows, the operator must install/enable WSL2 and Docker Desktop with
the Linux container engine, including any required reboot and license acceptance. This script
never installs them, starts a service, changes autostart, or registers a GitHub runner. Do not infer
that WSL2/Docker exists from an Omen deployment document.

- [Docker Desktop Windows prerequisites](https://docs.docker.com/desktop/setup/install/windows-install/)
- [uv installation](https://docs.astral.sh/uv/getting-started/installation/)

```powershell
.\scripts\local-ci.ps1 -CheckPrerequisites
.\scripts\local-ci.ps1 -Platform windows
.\scripts\local-ci.ps1 -Platform linux
```

The second and third commands request partial coverage. A successful partial command returns zero
but always lists the omitted platform as NOT RUN. A prerequisite-only success means tools were
found; it does not mean any checks passed. The host running the command is recorded; running on
another Windows computer does not constitute Omen deployment verification.

On a Linux development host, use an existing Python 3.11+ interpreter and uv/Docker on PATH:

```sh
python3 scripts/local_ci.py --platform linux
```

## What runs

Local jobs and both existing GitHub matrix jobs call `scripts/ci_checks.py`:

```text
uv lock --check
uv sync --frozen --group dev
uv run --frozen ruff check src tests
uv run --frozen mypy --platform linux src
uv run --frozen mypy --platform win32 src
uv run --frozen pytest
git diff --check
git diff --cached --check
git status --short (failure on tracked or untracked changes)
```

`uv lock --check` checks manifest/lock consistency; `--frozen` by itself trusts the lockfile rather
than checking whether the manifest changed. Checks stop on the first failing command, but the
checkout-cleanliness gate also runs after a command failure. The same lint and both type-check
targets also cover the three CI Python helper files. Skips remain visible in pytest output;
they must not be described as exercised WSL, model-server or symlink coverage.

Linux dependencies are prepared during the Docker build. Verification then runs offline with no
network, no host mounts, no Docker socket, dropped capabilities, no privilege escalation, a
non-root user, 4 CPUs, 8 GiB memory and a 512-process limit. Image/tool identities are printed in
the build log. Each streamed command has a 30-minute limit, including output drainage; cleanup
allows up to ten additional seconds for process/reader shutdown. Windows children start suspended
and join a kill-on-close Job object before resuming, so descendants cannot outlive the job merely
by keeping stdout open. Linux uses process groups inside the disposable container. Deliberately
detached native POSIX sessions are outside a process-group boundary; hostile native code needs
OS isolation. Docker cleanup queries also have explicit deadlines and fail if cleanup is unverified.

The uv/Python image is pinned by digest in `scripts/ci/Dockerfile`. Its uv version is 0.8.15; the
current repository lock was checked with that version during development. Debian Git installation
uses the base distribution's package repository and is not a bit-for-bit frozen apt snapshot.
Native Python/uv versions are recorded and may differ. Deliberately update the image digest and
re-run qualification when advancing these tools; never substitute `latest`.

## Isolation and limits

Only `git archive` of the clean committed HEAD is copied. Ignored/untracked `.env` files, venvs,
databases and the real `.git` directory never enter the job. Tracked dotenv/private-key/database
files, symbolic links and unsafe archive paths are refused. Each job receives a new environment
allowlist, temporary home, caches and database defaults. Broker credentials, model endpoints,
GitHub tokens, custom uv indexes and operator runtime variables are not forwarded. Git source
inspection preserves its line-ending normalization; temporary Git metadata has no remote,
credentials, global configuration or operator hooks.

Native Windows tests run as the current OS user. This is trusted-code verification with isolated
inputs, **not an OS security sandbox for hostile tests**: user filesystem access and local network
reachability remain possible to arbitrary native code. Do not run untrusted PR code this way;
use a disposable VM without operational access for adversarial code. There is no operational
broker credential/config, database or control endpoint supplied by this runner, and it never calls
an order, kill-switch, deployment or service-management command.

## Why direct Docker instead of act or a self-hosted runner

The workflow's verification is ordinary uv/Python/Git commands. Sharing that script avoids a second
command truth without registering a service or reproducing action orchestration unnecessarily.

| Approach | Decision |
| --- | --- |
| Direct short-lived Docker + shared commands | Selected. Small tool surface; explicit isolation; native Windows is separate. |
| act | Evaluated, not required for this command-only workflow. Linux images do not reproduce Windows or the full hosted image. Adds another runner and image mapping to maintain. |
| Containerized GitHub self-hosted runner | Deferred. Adds registration-token lifecycle, job-routing policy and persistent host exposure. No deployment workflow can accidentally route here because no runner is registered. |

Sources: [act runner limits](https://nektosact.com/usage/runners.html),
[GitHub self-hosted runner security](https://docs.github.com/en/actions/reference/security/secure-use),
[uv container pinning](https://docs.astral.sh/uv/guides/integration/docker/).

The Linux image is Debian-based, not the complete `ubuntu-latest` image; it reproduces the Linux
command suite only. Native Windows proves behavior on that host, not the complete `windows-latest`
image. Running mypy's Windows target inside Linux does not execute Windows code. No local result
publishes or impersonates `checks (ubuntu-latest)` or `checks (windows-latest)`.

## Hosted authority

The existing GitHub workflow remains manual-only as configured before this change. Its matrix,
job names, permissions and triggers are preserved; the command steps now call the shared script.
No hosted run is dispatched by this tool. Restoring automatic hosted triggers, publishing status
checks or changing branch protection is a separate explicit change. Local self-verification does
not replace an independent review/merge gate.

## Cleanup and recovery

Temporary source/home/cache directories are removed at job completion. Owned containers and
temporary image tags are removed in `finally`; build cache may remain for Docker to reuse.
Checkout contents are untouched except the ignored log directory. Commit changes before running;
the tool deliberately refuses to silently test an older HEAD when the working copy is dirty.

If interrupted by force-closing the host or a Docker daemon failure, the Linux log records the
exact generated container and image names. Inspect and remove only those names:

```text
docker container inspect <exact-owned-container-name>
docker container rm --force <exact-owned-container-name>
docker image inspect <exact-owned-image-name>
docker image rm <exact-owned-image-name>
```

Do not use global Docker prune or delete another task's container. Temporary folders use the
`elrond-local-ci-` prefix under the system temp directory; inspect the exact orphaned folder before
removing it after confirming no associated job is running. Logs can be removed by deleting an
individual completed `.local-ci/<run-id>` directory. A rerun creates fresh job state, so a failed
test cannot contaminate the next checkout. Repair Docker/WSL through its normal operator controls,
then rerun prerequisite detection; never turn an unavailable Linux check into a pass.
## Qualification record (2026-09-06)

Code tested: `ae5502688c3bba133ad442a8bcde872ae6968856`, on the available Windows host.
This is not a measurement of an Omen deployment or a native Pi run.

| Check | Measured result |
| --- | --- |
| Focused CI regression tests | 21 passed, including actual bootstrap UV configuration isolation and Windows descendant termination |
| Ruff for src/tests and CI helpers | Passed |
| Mypy for linux and win32 | Passed, 225 application files and 3 CI helper files |
| Default PowerShell entry point | Exit 1; Linux NOT RUN, Windows FAIL; selected commit retained in summary |
| Full native snapshot pytest | 2695 passed, 7 skipped, 2 failed, one existing deprecation warning; 393.52 seconds |
| Git cleanliness and temporary checkout removal | Passed; temporary Windows checkout absent after exit |
| Docker image build/execution | NOT RUN: Docker executable absent |

The final failing tests were
`test_a_registration_reaches_a_manifest_through_generated_code` (WSL could not prepare a workspace)
and `test_the_repository_is_absent_even_when_the_exact_path_is_supplied` (WSL service returned
`Wsl/Service/E_UNEXPECTED` during the control read). The unmodified baseline at `6ece2bf` also
failed WSL workspace/control-read checks: 2673 passed, 8 skipped, 2 failed. A preceding native run
hit the same WSL service error and a leftover sandbox-workspace assertion. These remain visible
failures; no test was weakened or forced to skip to produce a green CI report.

Final skips: four unconfigured real-model tests, two unprivileged-symlink tests, and the bootstrap
integration probe because the temporary home has no separately installed managed Python. That
bootstrap probe was exercised successfully in the separate 21-test host run. Raw logs and the
JSON summary remain locally under `.local-ci/20260906T220919Z-a4eafa5d/`; private machine paths
are not copied into GitHub. Keep #218 open pending actual Linux-container qualification and
resolution of native WSL failures. Draft PR: #224.
