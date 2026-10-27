# Sandboxed Test Runs — Design (Phase 7, part 2)

Status: implemented in apps/api · 2026-10-26 · Source: CODEMIND blueprint v1.0 §3.3, §13.1–13.2, §15 (Phase 7)

## 1. Goal and scope

Show whether a proposed patch passes the repository's tests, with results that reflect what really ran. Blueprint
exit gate for Phase 7: test status reflects actual execution. Builds on patch proposals (docs/patch-design.md).

In scope: a disposable patched workspace, two fixed test profiles, a locked-down Docker runner, bounded logs, a
`test_runs` table and API. Out of scope: installing a repository's own dependencies, a baseline run on the unpatched
code, stronger isolation than Docker (gVisor, microVMs) and per-user quotas, all needed before any public launch.

## 2. Flow

1. `POST /api/v1/patches/{id}/test-runs {"profile": "pytest"}` on a `validated`, `approved` or `exported` patch.
   The run stores the patch's `diff_sha256`, the image, the exact command and the limits, then is queued on the RQ
   `sandbox` queue. The worker (`python worker.py`) serves it after any indexing jobs.
2. The worker checks the patch's edits again against the snapshot. If the result is not the diff that was
   requested, the run ends as `error/patch_changed` and nothing executes.
3. It writes every indexed file of the snapshot into a fresh temporary folder, with the patched files replaced and
   created files added. Each write is checked to stay inside the folder. Files excluded from indexing (secrets,
   binaries, large or generated files) are not stored, so they are missing; `workspace_stats.excluded_files_missing`
   says how many.
4. It runs the profile in a container (below), then deletes the folder whatever happened.
5. `GET /api/v1/test-runs/{id}` and `GET /api/v1/patches/{id}/test-runs` return the result.

## 3. Profiles

Commands come only from this server-side list, never from the model or the request:

| Profile | Image (setting) | Command |
|---|---|---|
| `pytest` | `SANDBOX_PYTHON_IMAGE` = `codemind-sandbox-python:1` | `python -m pytest -q --color=no -p no:cacheprovider` with `PYTHONPATH=/work` |
| `node-test` | `SANDBOX_NODE_IMAGE` = `codemind-sandbox-node:1` | `node --test` |

Images are built ahead of time from `sandbox/*.Dockerfile`. Runs use `--pull never`, so a run cannot download
anything. A missing image is reported as `error/sandbox_start_failed` with the build command to run.

## 4. Container restrictions (blueprint §13.2)

| Control | How |
|---|---|
| No network | `--network none` |
| Not root, no privileges | `--user 65534:65534`, `--cap-drop ALL`, `--security-opt no-new-privileges`, never `--privileged` |
| Only the workspace is mounted, read-only | `--mount type=bind,…,target=/work,readonly`; no home folder, SSH agent, credentials or Docker socket |
| Read-only root filesystem | `--read-only`, plus a `noexec` `/tmp` tmpfs (`SANDBOX_TMP_MB`, 256 MB) |
| CPU, memory, processes | `--cpus` (1), `--memory` = `--memory-swap` (1024 MB), `--pids-limit` (256) |
| Wall clock | The runner kills the container (`docker kill`, not only the client) after `SANDBOX_TIMEOUT_SECONDS` (300) |
| Output | Written to a temp file; the run is stopped past 10 MB, and the last 64 KB are stored |
| Clean up | `--rm` for the container, the workspace folder is deleted in a `finally` |

Tests in `tests/test_sandbox.py` check these against real containers when Docker and the image are present: a
network connection fails, the user id is 65534, `/work` and `/etc` are not writable, and a sleeping test is
stopped at the timeout with no container left behind. Docker containers share the host kernel; that is acceptable
for running a developer's own repository locally, not for hostile multi-tenant code.

## 5. Statuses

| Status | Meaning |
|---|---|
| `queued`, `running` | Not finished |
| `passed` | The command ran and exited 0 |
| `failed` | The command ran and exited non-zero |
| `no_tests` | The test runner found no tests (pytest exit 5) |
| `timed_out` | Stopped at the time limit |
| `error` | Could not run or was stopped: `docker_not_found`, `sandbox_start_failed`, `patch_changed`, `workspace_too_large`, `output_limit`, `worker_lost`, `internal_error` |
| `blocked` | `SANDBOX_ENABLED` is false; the patch was not tested |

Nothing other than an exit code of 0 from a command that really ran is ever `passed`. A run still `running` five
minutes past its timeout is shown as `error/worker_lost` the next time it is read.

## 6. Data

`test_runs`: patch, profile, tested diff hash, status, image, command, limits, exit code, duration, bounded output
and whether it was cut, workspace stats, error, and created/started/finished times. Cascades with its patch. Each
request is also written to `audit_events` as `patch.test_run_requested`.
