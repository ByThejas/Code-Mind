"""Test a patch: write a disposable patched copy of the snapshot, run a fixed profile, record it.

Statuses report what actually happened (blueprint §13.1): `passed` only when the command ran and
exited 0. A run that could not execute is `error` or `blocked`, never `passed`. The copy is deleted
when the run ends, whatever the outcome.
"""

import logging
import shutil
import stat
import tempfile
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from codemind.core.config import Settings
from codemind.core.errors import CodemindError
from codemind.db.base import utcnow
from codemind.db.models import (
    AuditEvent,
    File,
    FileContent,
    Patch,
    PatchStatus,
    TestRun,
    TestRunStatus,
)
from codemind.patches.edits import Edit, check_edits
from codemind.patches.service import LOCAL_ACTOR, patch_limits, snapshot_files
from codemind.sandbox.docker import Limits, Profile, RunResult, SandboxRunner, profiles

log = logging.getLogger(__name__)

TESTABLE_PATCH_STATUSES = (PatchStatus.VALIDATED, PatchStatus.APPROVED, PatchStatus.EXPORTED)
LOST_AFTER_SECONDS = 300  # past the timeout, a running run whose worker died is marked lost

Enqueue = Callable[[str], None]


class WorkspaceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def create_test_run(
    db: Session, patch: Patch, profile_name: str, settings: Settings, enqueue: Enqueue
) -> TestRun:
    available = profiles(settings)
    if profile_name not in available:
        raise CodemindError(
            "unknown_profile",
            f"Unknown test profile. Choose one of: {', '.join(sorted(available))}.",
            status_code=422,
        )
    if patch.status not in TESTABLE_PATCH_STATUSES or patch.diff_sha256 is None:
        raise CodemindError(
            "invalid_patch_state", f"A {patch.status} patch cannot be tested.", status_code=409
        )
    profile = available[profile_name]
    limits = Limits.from_settings(settings)
    run = TestRun(
        workspace_id=patch.workspace_id,
        patch_id=patch.id,
        profile=profile.name,
        diff_sha256=patch.diff_sha256,
        image=profile.image,
        command=list(profile.command),
        limits=limits.to_dict(),
        workspace_stats={},
    )
    if not settings.sandbox_enabled:
        run.status = TestRunStatus.BLOCKED
        run.error_code = "sandbox_disabled"
        run.error_message = (
            "Running tests is turned off. Set SANDBOX_ENABLED=true after building the sandbox "
            "images (docs/sandbox-design.md). The patch has not been tested."
        )
        run.finished_at = utcnow()
    db.add(run)
    db.flush()
    db.add(
        AuditEvent(
            workspace_id=patch.workspace_id,
            actor=LOCAL_ACTOR,
            action="patch.test_run_requested",
            resource_type="patch",
            resource_id=patch.id,
            details={"test_run_id": str(run.id), "profile": profile.name, "status": run.status},
        )
    )
    db.commit()
    if run.status == TestRunStatus.QUEUED:
        enqueue(str(run.id))
    return run


def mark_lost_runs(db: Session, run: TestRun, settings: Settings) -> None:
    """A run still `running` long after its timeout lost its worker; say so instead of spinning."""
    if run.status != TestRunStatus.RUNNING or run.started_at is None:
        return
    deadline = run.started_at + timedelta(
        seconds=settings.sandbox_timeout_seconds + LOST_AFTER_SECONDS
    )
    if utcnow() > deadline:
        run.status = TestRunStatus.ERROR
        run.error_code = "worker_lost"
        run.error_message = "The worker running this test stopped responding."
        run.finished_at = utcnow()
        db.commit()


def execute_test_run(
    run_id: uuid.UUID,
    session_factory: sessionmaker[Session],
    runner: SandboxRunner,
    settings: Settings,
) -> None:
    with session_factory() as db:
        run = db.get(TestRun, run_id, with_for_update=True)
        if run is None or run.status != TestRunStatus.QUEUED:
            return
        run.status = TestRunStatus.RUNNING
        run.started_at = utcnow()
        db.commit()

        patch = db.get(Patch, run.patch_id)
        profile = profiles(settings).get(run.profile)
        root = Path(tempfile.mkdtemp(prefix="codemind-run-"))
        try:
            if patch is None or profile is None:
                raise WorkspaceError("patch_not_found", "The patch or profile no longer exists.")
            run.workspace_stats = write_workspace(db, patch, root, settings)
            result = runner.run(
                root, profile, Limits.from_settings(settings), f"codemind-run-{run.id}"
            )
        except WorkspaceError as exc:
            run.status = TestRunStatus.ERROR
            run.error_code, run.error_message = exc.code, exc.message
        except Exception:
            log.exception("test run %s crashed", run.id)
            run.status = TestRunStatus.ERROR
            run.error_code, run.error_message = "internal_error", "The test run crashed."
        else:
            _record(run, result, profile)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        run.finished_at = utcnow()
        db.commit()


def _record(run: TestRun, result: RunResult, profile: Profile) -> None:
    run.exit_code = result.exit_code
    run.duration_ms = result.duration_ms
    run.output = result.output
    run.output_truncated = result.output_truncated
    if result.error_code:
        run.status = TestRunStatus.ERROR
        run.error_code, run.error_message = result.error_code, result.error_message
    elif result.timed_out:
        run.status = TestRunStatus.TIMED_OUT
        run.error_code = "timed_out"
        run.error_message = f"Stopped after {run.limits.get('timeout_seconds')} seconds."
    elif result.output_limit_hit:
        run.status = TestRunStatus.ERROR
        run.error_code = "output_limit"
        run.error_message = "Stopped because the command wrote too much output."
    elif result.exit_code == 0:
        run.status = TestRunStatus.PASSED
    elif result.exit_code in profile.no_tests_exit_codes:
        run.status = TestRunStatus.NO_TESTS
    else:
        run.status = TestRunStatus.FAILED


def write_workspace(db: Session, patch: Patch, root: Path, settings: Settings) -> dict[str, int]:
    """Write the snapshot's indexed files with the patch applied under `root`.

    The edits are checked again against the snapshot, and the result must be the diff that was
    requested for testing. Files excluded from indexing (secrets, binaries, large files) are not
    stored, so they are missing from the copy; tests that need them will fail, and the stats say
    how many were left out.
    """
    edits = [Edit(e["path"], e["old_text"], e["new_text"]) for e in patch.edits]
    check = check_edits(edits, snapshot_files(db, patch.snapshot_id, edits), patch_limits(settings))
    if not check.ok or check.diff_sha256 != patch.diff_sha256:
        raise WorkspaceError("patch_changed", "The patch no longer applies as it was reviewed.")
    patched = {f.path: f.new_text for f in check.files}

    root_resolved = root.resolve()
    written = total = 0
    rows = db.execute(
        select(File.path, FileContent.text)
        .join(FileContent, FileContent.file_id == File.id)
        .where(File.snapshot_id == patch.snapshot_id, File.excluded_reason.is_(None))
        .execution_options(yield_per=500)
    )
    for path, text in rows:
        content = patched.pop(path, text)
        total += _write(root_resolved, path, content)
        written += 1
        if total > settings.sandbox_max_workspace_bytes:
            raise WorkspaceError("workspace_too_large", "The repository is too large to test.")
    for path, content in patched.items():  # files the patch creates
        total += _write(root_resolved, path, content)
        written += 1
    excluded = db.scalar(
        select(func.count())
        .select_from(File)
        .where(File.snapshot_id == patch.snapshot_id, File.excluded_reason.is_not(None))
    )
    _open_up(root_resolved)
    return {
        "files_written": written,
        "bytes_written": total,
        "patched_files": len(check.files),
        "excluded_files_missing": excluded or 0,
    }


def _write(root: Path, path: str, content: str) -> int:
    target = (root / path).resolve()
    if not target.is_relative_to(root):  # paths are checked at indexing; never trust that alone
        raise WorkspaceError("invalid_path", f"Refusing to write outside the workspace: {path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode()
    target.write_bytes(data)
    return len(data)


def _open_up(root: Path) -> None:
    """Let the container's unprivileged user read the copy (mkdtemp makes it owner-only)."""
    for path in [root, *root.rglob("*")]:
        mode = path.stat().st_mode
        extra = stat.S_IRGRP | stat.S_IROTH
        if path.is_dir():
            extra |= stat.S_IXGRP | stat.S_IXOTH
        path.chmod(mode | extra)
