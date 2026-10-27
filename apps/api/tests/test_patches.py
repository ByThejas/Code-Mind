"""Patch proposals: edit checking, the diff, and the approval state machine.

The model is faked; CI never runs one. Diffs are checked with real `git apply` where git exists.
"""

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from codemind.api.dependencies import get_answer_model, get_embedding_provider
from codemind.core.errors import CodemindError
from codemind.db.models import AuditEvent, Patch, Workspace
from codemind.main import app
from codemind.patches.edits import (
    Edit,
    PatchLimits,
    PathError,
    SnapshotFile,
    check_edits,
    normalize_path,
)
from codemind.providers import GeneratedAnswer, GeneratedPatch
from codemind.schemas.patches import PatchDraft, PatchEditDraft
from codemind.schemas.retrieval import AnswerDraft
from tests.fixtures import APP_PY
from tests.test_retrieval import HashEmbeddings, _index

APP = APP_PY.decode()
FILES = {"src/app.py": SnapshotFile(APP), "logo.png": SnapshotFile(None, "binary")}

# Edit checking


def _codes(issues) -> list[str]:
    return [i.code for i in issues]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("../etc/passwd", "invalid_path"),
        ("src/../../x.py", "invalid_path"),
        ("/etc/passwd", "invalid_path"),
        ("C:/Windows/x.py", "invalid_path"),
        ("src\\app.py", "invalid_path"),
        ("src//app.py", "invalid_path"),
        ("src/./app.py", "invalid_path"),
        ("", "invalid_path"),
        ("src/a\x00.py", "invalid_path"),
        ("src/\napp.py", "invalid_path"),
        (".git/config", "sensitive_path"),
        ("sub/.git/hooks/pre-commit", "sensitive_path"),
        (".env", "sensitive_path"),
        ("deploy/id_rsa", "sensitive_path"),
        ("config/secrets.yaml", "sensitive_path"),
        ("node_modules/x/index.js", "excluded_path"),
    ],
)
def test_paths_that_could_escape_or_leak_are_refused(raw: str, code: str) -> None:
    with pytest.raises(PathError) as exc:
        normalize_path(raw)
    assert exc.value.code == code


def test_normalize_path_accepts_plain_relative_paths() -> None:
    assert normalize_path("src/app.py") == "src/app.py"
    assert normalize_path("./src/app.py") == "src/app.py"
    assert normalize_path(".env.example") == ".env.example"


def test_a_valid_edit_gives_a_diff_with_counts() -> None:
    check = check_edits(
        [Edit("src/app.py", 'return f"hello {name}"', 'return f"hi {name}"')], FILES
    )
    assert check.ok and not check.warnings
    [file] = check.files
    assert (file.path, file.status, file.additions, file.deletions) == (
        "src/app.py",
        "modified",
        1,
        1,
    )
    assert check.diff.startswith("diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n")
    assert '-    return f"hello {name}"\n+    return f"hi {name}"\n' in check.diff
    assert len(check.diff_sha256) == 64


def test_edits_apply_in_order_and_can_create_files() -> None:
    check = check_edits(
        [
            Edit("src/app.py", "def greet(name):", "def greet(name, punctuation=''):"),
            Edit("src/app.py", 'return f"hello {name}"', 'return f"hello {name}{punctuation}"'),
            Edit("tests/test_app.py", "", "def test_greet():\n    assert True\n"),
        ],
        FILES,
    )
    assert check.ok, check.errors
    assert [(f.path, f.status) for f in check.files] == [
        ("src/app.py", "modified"),
        ("tests/test_app.py", "added"),
    ]
    assert "new file mode 100644\n--- /dev/null\n+++ b/tests/test_app.py\n" in check.diff


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (Edit("src/app.py", "greet", "hello"), "old_text_ambiguous"),
        (Edit("src/app.py", "def nothing():", "x"), "old_text_not_found"),
        (Edit("src/app.py", "", "replaced"), "old_text_required"),
        (Edit("src/app.py", "import os", "import os"), "no_change"),
        (Edit("src/missing.py", "x = 1", "x = 2"), "file_not_found"),
        (Edit("logo.png", "PNG", "JPG"), "file_excluded"),
        (Edit("../app.py", "x", "y"), "invalid_path"),
        (
            Edit(
                "src/app.py", "def greet(name):", 'KEY = "AKIAABCDEFGHIJKLMNOP"\ndef greet(name):'
            ),
            "secret_content",
        ),
    ],
)
def test_bad_edits_are_reported_with_their_index(edit: Edit, code: str) -> None:
    check = check_edits([Edit("src/app.py", "import os", "import sys"), edit], FILES)
    assert not check.ok
    [issue] = check.errors
    assert issue.code == code
    if code != "secret_content":
        assert issue.edit_index == 1


def test_trailing_whitespace_and_crlf_differences_still_match_with_a_warning() -> None:
    old = "class Greeter:  \r\n    def __init__(self, prefix):\r\n"
    check = check_edits(
        [Edit("src/app.py", old, "class Greeter:\n    def __init__(self, p):")], FILES
    )
    assert check.ok, check.errors
    assert _codes(check.warnings) == ["matched_ignoring_whitespace"]
    assert "+    def __init__(self, p):\n         self.prefix = prefix\n" in check.diff


def test_limits_are_enforced() -> None:
    many = [Edit(f"new{i}.py", "", "x = 1\n") for i in range(3)]
    assert _codes(check_edits(many, FILES, PatchLimits(max_edits=2)).errors) == ["too_many_edits"]
    assert _codes(check_edits(many, FILES, PatchLimits(max_files=2)).errors) == ["too_many_files"]
    big = [Edit("new.py", "", "x = 1\n" * 50)]
    assert _codes(check_edits(big, FILES, PatchLimits(max_changed_lines=10)).errors) == [
        "too_many_changed_lines"
    ]
    assert _codes(check_edits(big, FILES, PatchLimits(max_file_bytes=20)).errors) == [
        "file_too_large"
    ]
    assert _codes(check_edits([], FILES).errors) == ["no_edits"]


def test_a_model_may_only_edit_files_it_was_shown() -> None:
    edit = Edit("src/app.py", "import os", "import sys")
    check = check_edits([edit], FILES, allowed_paths={"web/index.ts"})
    assert _codes(check.errors) == ["path_not_in_evidence"]
    new_file = Edit("src/new.py", "", "x = 1\n")
    assert check_edits([new_file], FILES, allowed_paths=set()).ok


def test_missing_final_newline_is_marked_in_the_diff() -> None:
    files = {"a.txt": SnapshotFile("one\ntwo")}
    check = check_edits([Edit("a.txt", "two", "three")], files)
    assert check.ok
    assert (
        "-two\n\\ No newline at end of file\n+three\n\\ No newline at end of file\n" in check.diff
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_apply_accepts_the_diff(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_bytes(APP_PY)
    (tmp_path / "a.txt").write_text("one\ntwo")
    files = FILES | {"a.txt": SnapshotFile("one\ntwo")}
    check = check_edits(
        [
            Edit(
                "src/app.py", "        self.prefix = prefix", "        self.prefix = prefix or ''"
            ),
            Edit("a.txt", "two", "three"),
            Edit("docs/notes.md", "", "# Notes\n"),
        ],
        files,
    )
    assert check.ok, check.errors
    (tmp_path / "change.diff").write_text(check.diff)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "apply", "change.diff"], cwd=tmp_path, check=True)
    assert "self.prefix = prefix or ''" in (tmp_path / "src/app.py").read_text()
    assert (tmp_path / "a.txt").read_text() == "one\nthree"
    assert (tmp_path / "docs/notes.md").read_text() == "# Notes\n"


# API


@pytest.fixture
def providers() -> Iterator[dict[str, object]]:
    chosen: dict[str, object] = {"embedder": None, "model": None}
    app.dependency_overrides[get_embedding_provider] = lambda: chosen["embedder"]
    app.dependency_overrides[get_answer_model] = lambda: chosen["model"]
    yield chosen


class FakeModel:
    model_name = "test-model"

    def __init__(self, patch: PatchDraft | None = None, error: CodemindError | None = None):
        self.patch, self.error = patch, error
        self.prompts: list[tuple[str, str]] = []

    def answer(self, system: str, user: str) -> GeneratedAnswer:
        draft = AnswerDraft(
            summary="greet says hello.", claims=[], limitations=[], suggested_next_steps=[]
        )
        return GeneratedAnswer(draft, self.model_name)

    def propose_patch(self, system: str, user: str) -> GeneratedPatch:
        self.prompts.append((system, user))
        if self.error:
            raise self.error
        assert self.patch is not None
        return GeneratedPatch(self.patch, self.model_name, input_tokens=50, output_tokens=10)


def _investigation(client, session_factory, enqueued, providers) -> str:
    repo_id = _index(client, session_factory, enqueued, HashEmbeddings())
    providers["model"] = FakeModel()
    r = client.post(
        f"/api/v1/repositories/{repo_id}/investigations", json={"question": "How does greet work?"}
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


GOOD_EDIT = {
    "path": "src/app.py",
    "old_text": 'return f"hello {name}"',
    "new_text": 'return f"hi {name}"',
}


def test_manual_patch_lifecycle(client, session_factory, enqueued, providers, db: Session) -> None:
    inv = _investigation(client, session_factory, enqueued, providers)
    url = f"/api/v1/investigations/{inv}/patches"
    r = client.post(url, json={"instruction": "Say hi instead", "edits": [GOOD_EDIT]})
    assert r.status_code == 201, r.text
    patch = r.json()
    assert patch["status"] == "validated" and patch["source"] == "manual"
    assert patch["commit_sha"] == "a" * 40
    assert patch["files"] == [
        {"path": "src/app.py", "status": "modified", "additions": 1, "deletions": 1}
    ]
    assert patch["validation"] == {"errors": [], "warnings": []}
    pid = patch["id"]

    # Export and approval are gated.
    assert client.post(f"/api/v1/patches/{pid}/export").json()["error"]["code"] == (
        "patch_not_approved"
    )
    wrong = client.post(f"/api/v1/patches/{pid}/approve", json={"diff_sha256": "0" * 64})
    assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "diff_mismatch"

    approved = client.post(
        f"/api/v1/patches/{pid}/approve", json={"diff_sha256": patch["diff_sha256"]}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["decided_by"] == "local-user"
    again = client.post(
        f"/api/v1/patches/{pid}/approve", json={"diff_sha256": patch["diff_sha256"]}
    )
    assert again.json()["error"]["code"] == "invalid_patch_state"

    exported = client.post(f"/api/v1/patches/{pid}/export")
    assert exported.status_code == 200
    body = exported.json()
    assert body["diff"] == patch["diff"] and body["filename"].endswith("-aaaaaaa.diff")
    assert client.get(f"/api/v1/patches/{pid}").json()["status"] == "exported"
    assert client.post(f"/api/v1/patches/{pid}/reject", json={}).status_code == 409

    listed = client.get(url).json()["items"]
    assert [p["id"] for p in listed] == [pid]

    actions = db.scalars(select(AuditEvent.action).order_by(AuditEvent.created_at)).all()
    assert actions == ["patch.created", "patch.approved", "patch.exported"]
    approval = db.scalars(select(AuditEvent).where(AuditEvent.action == "patch.approved")).one()
    assert approval.details == {"diff_sha256": patch["diff_sha256"]}


def test_invalid_patch_is_stored_with_reasons_and_cannot_be_approved(
    client,
    session_factory,
    enqueued,
    providers,
) -> None:
    inv = _investigation(client, session_factory, enqueued, providers)
    r = client.post(
        f"/api/v1/investigations/{inv}/patches",
        json={
            "instruction": "Break out",
            "edits": [GOOD_EDIT, {"path": "../../etc/passwd", "old_text": "", "new_text": "x"}],
        },
    )
    assert r.status_code == 201
    patch = r.json()
    assert patch["status"] == "invalid" and patch["diff"] is None
    assert patch["validation"]["errors"][0]["code"] == "invalid_path"
    assert patch["validation"]["errors"][0]["edit_index"] == 1
    approve = client.post(f"/api/v1/patches/{patch['id']}/approve", json={"diff_sha256": "0" * 64})
    assert approve.json()["error"]["code"] == "invalid_patch_state"
    rejected = client.post(f"/api/v1/patches/{patch['id']}/reject", json={"reason": "unsafe"})
    assert rejected.json()["status"] == "rejected"


def test_model_patch_uses_evidence_and_is_checked(
    client,
    session_factory,
    enqueued,
    providers,
) -> None:
    inv = _investigation(client, session_factory, enqueued, providers)
    url = f"/api/v1/investigations/{inv}/patches"

    providers["model"] = None
    unconfigured = client.post(url, json={"instruction": "Say hi in greet"})
    assert unconfigured.status_code == 503

    model = FakeModel(
        PatchDraft(
            summary="greet says hi.",
            edits=[PatchEditDraft(**GOOD_EDIT)],
            limitations=["Callers were not checked."],
        )
    )
    providers["model"] = model
    r = client.post(url, json={"instruction": "Say hi in greet"})
    assert r.status_code == 201, r.text
    patch = r.json()
    assert patch["status"] == "validated" and patch["source"] == "model"
    assert patch["summary"] == "greet says hi."
    assert patch["limitations"] == ["Callers were not checked."]
    meta = patch["metadata"]
    assert meta["prompt_version"] == "patch-v1" and meta["input_tokens"] == 50
    assert "src/app.py" in {e["path"] for e in meta["evidence"]}
    system, user = model.prompts[0]
    assert "untrusted" in system and "Change requested: Say hi in greet" in user
    assert "Answer given: greet says hello." in user


def test_model_failure_is_stored_then_reported(
    client,
    session_factory,
    enqueued,
    providers,
    db: Session,
) -> None:
    inv = _investigation(client, session_factory, enqueued, providers)
    providers["model"] = FakeModel(
        error=CodemindError("answer_provider_error", "Ollama is not reachable.", status_code=503)
    )
    r = client.post(
        f"/api/v1/investigations/{inv}/patches", json={"instruction": "Say hi in greet"}
    )
    assert r.status_code == 503
    stored = db.scalars(select(Patch)).one()
    assert stored.status == "failed" and stored.error_code == "answer_provider_error"


def test_patches_are_workspace_scoped(client, session_factory, enqueued, providers, db) -> None:
    inv = _investigation(client, session_factory, enqueued, providers)
    created = client.post(
        f"/api/v1/investigations/{inv}/patches",
        json={"instruction": "Say hi", "edits": [GOOD_EDIT]},
    ).json()
    other = Workspace(name="Other")
    db.add(other)
    db.commit()
    db.execute(Patch.__table__.update().values(workspace_id=other.id))
    db.commit()
    assert client.get(f"/api/v1/patches/{created['id']}").status_code == 404
    assert client.post(f"/api/v1/patches/{created['id']}/export").status_code == 404
