"""Test runs: the disposable workspace, status reporting, and the Docker sandbox's restrictions.

Most tests use a fake runner. The ones marked `docker` run real containers and are skipped unless
Docker is running and the sandbox image is built (docs/sandbox-design.md).
"""

import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from codemind.api.dependencies import get_enqueue_test_run
from codemind.core.config import settings
from codemind.db.models import AuditEvent, Patch, TestRun
from codemind.main import app
from codemind.sandbox.docker import DockerRunner, Limits, Profile, RunResult, profiles
from codemind.sandbox.runs import execute_test_run
from tests.test_patches import GOOD_EDIT, _investigation, providers  # noqa: F401

PASSING_TEST = "from src.app import greet\n\n\ndef test_greet():\n    assert greet('x') == 'hi x'\n"
NEW_TEST = {"path": "tests/test_greet.py", "old_text": "", "new_text": PASSING_TEST}


@pytest.fixture
def queued() -> Iterator[list[str]]:
    ids: list[str] = []
    app.dependency_overrides[get_enqueue_test_run] = lambda: ids.append
    yield ids


@pytest.fixture
def sandbox_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sandbox_enabled", True)


class FakeRunner:
    def __init__(self, result: RunResult) -> None:
        self.result = result
        self.seen: dict[str, str] = {}
        self.profile: Profile | None = None

    def run(self, workspace: Path, profile: Profile, limits: Limits, name: str) -> RunResult:
        self.profile = profile
        self.seen = {
            str(p.relative_to(workspace)).replace("\\", "/"): p.read_text()
            for p in workspace.rglob("*")
            if p.is_file()
        }
        self.workspace = workspace
        return self.result


def _patch(client, session_factory, enqueued, providers, edits) -> str:  # noqa: F811
    inv = _investigation(client, session_factory, enqueued, providers)
    r = client.post(
        f"/api/v1/investigations/{inv}/patches", json={"instruction": "Say hi", "edits": edits}
    )
    assert r.status_code == 201 and r.json()["status"] == "validated", r.text
    return str(r.json()["id"])


def test_disabled_sandbox_records_a_blocked_run(
    client,
    session_factory,
    enqueued,
    providers,  # noqa: F811
    queued,
    db: Session,
) -> None:
    pid = _patch(client, session_factory, enqueued, providers, [GOOD_EDIT])
    r = client.post(f"/api/v1/patches/{pid}/test-runs", json={})
    assert r.status_code == 202, r.text
    run = r.json()
    assert run["status"] == "blocked" and run["error_code"] == "sandbox_disabled"
    assert run["exit_code"] is None and queued == []
    actions = db.scalars(select(AuditEvent.action)).all()
    assert "patch.test_run_requested" in actions


def test_requests_are_checked(
    client,
    session_factory,
    enqueued,
    providers,  # noqa: F811
    queued,
    sandbox_on,
) -> None:
    pid = _patch(client, session_factory, enqueued, providers, [GOOD_EDIT])
    bad = client.post(f"/api/v1/patches/{pid}/test-runs", json={"profile": "rm -rf /"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "unknown_profile"
    client.post(f"/api/v1/patches/{pid}/reject", json={})
    rejected = client.post(f"/api/v1/patches/{pid}/test-runs", json={})
    assert rejected.status_code == 409
    assert client.post(f"/api/v1/patches/{uuid.uuid4()}/test-runs", json={}).status_code == 404


@pytest.mark.parametrize(
    ("result", "status"),
    [
        (RunResult(0, "1 passed", False, 120), "passed"),
        (RunResult(1, "1 failed", False, 120), "failed"),
        (RunResult(5, "no tests ran", False, 120), "no_tests"),
        (RunResult(None, "", False, 3000, timed_out=True), "timed_out"),
        (RunResult(137, "", False, 900, output_limit_hit=True), "error"),
        (RunResult(125, "", False, 10, error_code="sandbox_start_failed"), "error"),
    ],
)
def test_run_statuses_reflect_what_happened(
    client,
    session_factory,
    enqueued,
    providers,  # noqa: F811
    queued,
    sandbox_on,
    result: RunResult,
    status: str,
) -> None:
    pid = _patch(client, session_factory, enqueued, providers, [GOOD_EDIT, NEW_TEST])
    created = client.post(f"/api/v1/patches/{pid}/test-runs", json={"profile": "pytest"}).json()
    assert created["status"] == "queued" and queued == [created["id"]]
    assert created["limits"]["network"] == 0

    runner = FakeRunner(result)
    execute_test_run(uuid.UUID(created["id"]), session_factory, runner, settings)
    run = client.get(f"/api/v1/test-runs/{created['id']}").json()
    assert run["status"] == status
    assert run["started_at"] and run["finished_at"]
    assert run["duration_ms"] == result.duration_ms
    assert not runner.workspace.exists()  # the copy is gone after the run

    # The copy holds the patched snapshot, but nothing that was excluded from indexing.
    assert 'return f"hi {name}"' in runner.seen["src/app.py"]
    assert runner.seen["tests/test_greet.py"] == PASSING_TEST
    assert "README.md" in runner.seen
    assert ".env" not in runner.seen and "leak.py" not in runner.seen
    assert run["workspace_stats"]["patched_files"] == 2
    assert run["workspace_stats"]["excluded_files_missing"] > 0

    listed = client.get(f"/api/v1/patches/{pid}/test-runs").json()["items"]
    assert [r["id"] for r in listed] == [created["id"]]

    # A run executes once.
    execute_test_run(uuid.UUID(created["id"]), session_factory, FakeRunner(result), settings)


def test_a_patch_that_no_longer_matches_its_diff_is_not_tested(
    client,
    session_factory,
    enqueued,
    providers,  # noqa: F811
    queued,
    sandbox_on,
    db: Session,
) -> None:
    pid = _patch(client, session_factory, enqueued, providers, [GOOD_EDIT])
    created = client.post(f"/api/v1/patches/{pid}/test-runs", json={}).json()
    patch = db.get(Patch, uuid.UUID(pid))
    assert patch is not None
    patch.edits = [GOOD_EDIT | {"new_text": 'return f"bye {name}"'}]
    db.commit()
    runner = FakeRunner(RunResult(0, "", False, 1))
    execute_test_run(uuid.UUID(created["id"]), session_factory, runner, settings)
    run = db.scalars(select(TestRun)).one()
    db.refresh(run)
    assert run.status == "error" and run.error_code == "patch_changed"
    assert runner.profile is None  # nothing ran


def test_docker_command_locks_the_container_down(tmp_path: Path) -> None:
    profile = profiles(settings)["pytest"]
    limits = Limits.from_settings(settings)
    argv = DockerRunner("docker").command(tmp_path, profile, limits, "codemind-run-x")
    joined = " ".join(argv)
    for flag in (
        "--rm",
        "--pull never",
        "--network none",
        "--user 65534:65534",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--read-only",
        f"--memory {limits.memory_mb}m",
        f"--pids-limit {limits.pids}",
        "target=/work,readonly",
    ):
        assert flag in joined
    assert "docker.sock" not in joined and "--privileged" not in joined
    assert argv[-len(profile.command) :] == list(profile.command)
    assert argv[-len(profile.command) - 1] == profile.image


def test_missing_docker_is_an_error_not_a_pass(tmp_path: Path) -> None:
    profile = profiles(settings)["pytest"]
    result = DockerRunner("no-such-docker-binary").run(
        tmp_path, profile, Limits.from_settings(settings), "x"
    )
    assert result.error_code == "docker_not_found" and result.exit_code is None


# Real containers


def _image_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    found = subprocess.run(
        ["docker", "image", "inspect", settings.sandbox_python_image],
        check=False,
        capture_output=True,
    )
    return found.returncode == 0


docker = pytest.mark.skipif(not _image_ready(), reason="Docker or the sandbox image is missing")


def _run_in_docker(tmp_path: Path, test_source: str, timeout: int = 60) -> RunResult:
    (tmp_path / "test_case.py").write_text(test_source)
    tmp_path.chmod(0o755)
    (tmp_path / "test_case.py").chmod(0o644)
    limits = Limits.from_settings(settings.model_copy(update={"sandbox_timeout_seconds": timeout}))
    return DockerRunner().run(
        tmp_path, profiles(settings)["pytest"], limits, f"codemind-test-{uuid.uuid4().hex[:8]}"
    )


@docker
def test_docker_reports_passing_and_failing_tests(tmp_path: Path) -> None:
    passed = _run_in_docker(tmp_path, "def test_ok():\n    assert 1 + 1 == 2\n")
    assert passed.exit_code == 0 and "1 passed" in passed.output
    failed = _run_in_docker(tmp_path, "def test_bad():\n    assert 1 + 1 == 3\n")
    assert failed.exit_code == 1 and "1 failed" in failed.output


@docker
def test_docker_has_no_network_no_root_and_a_read_only_workspace(tmp_path: Path) -> None:
    source = """import os, socket, pytest

def test_no_network():
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 53), timeout=3)

def test_not_root():
    assert os.getuid() == 65534

def test_workspace_is_read_only():
    with pytest.raises(OSError):
        open("/work/written.txt", "w")

def test_root_filesystem_is_read_only():
    with pytest.raises(OSError):
        open("/etc/evil", "w")
"""
    result = _run_in_docker(tmp_path, source)
    assert result.exit_code == 0, result.output
    assert "4 passed" in result.output
    assert not (tmp_path / "written.txt").exists()


@docker
def test_docker_run_is_stopped_at_the_timeout(tmp_path: Path) -> None:
    result = _run_in_docker(tmp_path, "import time\n\ndef test_slow():\n    time.sleep(60)\n", 3)
    assert result.timed_out and result.duration_ms < 40_000
    names = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"], check=False, capture_output=True, text=True
    ).stdout
    assert "codemind-test-" not in names  # the container was killed, not left running


@docker
def test_patched_snapshot_tests_run_end_to_end(
    client,
    session_factory,
    enqueued,
    providers,  # noqa: F811
    queued,
    sandbox_on,
) -> None:
    pid = _patch(client, session_factory, enqueued, providers, [GOOD_EDIT, NEW_TEST])
    created = client.post(f"/api/v1/patches/{pid}/test-runs", json={}).json()
    execute_test_run(uuid.UUID(created["id"]), session_factory, DockerRunner(), settings)
    run = client.get(f"/api/v1/test-runs/{created['id']}").json()
    assert run["status"] == "passed", run["output"]
    assert run["exit_code"] == 0 and "1 passed" in run["output"]
