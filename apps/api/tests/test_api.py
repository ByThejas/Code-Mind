import uuid
from collections.abc import Iterator

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from codemind.api.dependencies import get_db, get_enqueue, get_github
from codemind.db.models import Repository, Workspace
from codemind.indexing.pipeline import IndexRun
from codemind.main import app
from codemind.repositories.github import GitHubClient
from tests.fixtures import SHA, SHA2, make_tarball
from tests.test_pipeline import SETTINGS

API = "https://api.github.com"
REPO_JSON = {
    "owner": {"login": "owner"},
    "name": "repo",
    "default_branch": "main",
    "private": False,
    "size": 10,
}


@pytest.fixture
def enqueued() -> list[str]:
    return []


@pytest.fixture
def client(session_factory: sessionmaker[Session], enqueued: list[str]) -> Iterator[TestClient]:
    def db() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    def github() -> Iterator[GitHubClient]:
        with GitHubClient(SETTINGS) as gh:
            yield gh

    app.dependency_overrides.update(
        {get_db: db, get_github: github, get_enqueue: lambda: enqueued.append}
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{API}/repos/owner/repo").respond(json=REPO_JSON)
        mock.get(f"{API}/repos/owner/repo/commits/main").respond(json={"sha": SHA})
        mock.get(f"{API}/repos/owner/repo/commits/v2").respond(json={"sha": SHA2})
        mock.get(f"{API}/repos/owner/repo/tarball/{SHA}").respond(content=make_tarball())
        mock.get(f"{API}/repos/owner/private").respond(json=REPO_JSON | {"private": True})
        mock.route(host="testserver").pass_through()
        yield TestClient(app)
    app.dependency_overrides.clear()


def _work(session_factory: sessionmaker[Session], enqueued: list[str]) -> None:
    while enqueued:
        IndexRun(
            uuid.UUID(enqueued.pop(0)),
            session_factory=session_factory,
            github_factory=lambda: GitHubClient(SETTINGS),
            settings=SETTINGS,
        ).run()


def test_import_index_and_read(client: TestClient, session_factory, enqueued: list[str]) -> None:
    r = client.post("/api/v1/repositories", json={"url": "https://github.com/owner/repo"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["snapshot"]["commit_sha"] == SHA
    assert body["job"]["status"] == "queued"
    repo_id, job_id = body["repository"]["id"], body["job"]["id"]
    assert enqueued == [job_id]

    # A second import of the same commit while queued returns the same job, no new enqueue.
    again = client.post("/api/v1/repositories", json={"url": "owner/repo"})
    assert again.status_code == 202 and again.json()["job"]["id"] == job_id
    assert enqueued == [job_id]

    assert client.get(f"/api/v1/repositories/{repo_id}/files").json()["error"]["code"] == (
        "snapshot_not_ready"
    )

    _work(session_factory, enqueued)
    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert (job["status"], job["progress"]) == ("succeeded", 100)

    # Now that commit is indexed: 200 and no job.
    done = client.post("/api/v1/repositories", json={"url": "owner/repo"})
    assert done.status_code == 200 and done.json()["job"] is None

    detail = client.get(f"/api/v1/repositories/{repo_id}").json()
    assert detail["latest_snapshot"]["status"] == "partial"
    listed = client.get("/api/v1/repositories").json()
    assert [r["id"] for r in listed["items"]] == [repo_id] and listed["next_cursor"] is None

    files = client.get(f"/api/v1/repositories/{repo_id}/files").json()
    assert files["commit_sha"] == SHA
    assert [f["path"] for f in files["items"]] == sorted(
        [".env.example", "README.md", "src/app.py", "src/broken.py", "web/index.ts"]
    )
    page = client.get(f"/api/v1/repositories/{repo_id}/files", params={"limit": 2}).json()
    assert len(page["items"]) == 2 and page["next_cursor"] == page["items"][-1]["path"]
    src = client.get(f"/api/v1/repositories/{repo_id}/files", params={"path_prefix": "src/"}).json()
    assert [f["path"] for f in src["items"]] == ["src/app.py", "src/broken.py"]
    with_excluded = client.get(
        f"/api/v1/repositories/{repo_id}/files", params={"include_excluded": True, "limit": 500}
    ).json()
    assert {f["path"]: f["excluded_reason"] for f in with_excluded["items"]}[".env"] == (
        "secret_path"
    )

    content = client.get(
        f"/api/v1/repositories/{repo_id}/files/content",
        params={"path": "src/app.py", "start_line": 4, "end_line": 6},
    ).json()
    assert content["text"] == 'def greet(name):\n    """Say hello."""\n    return f"hello {name}"\n'
    assert (content["start_line"], content["end_line"], content["commit_sha"]) == (4, 6, SHA)

    secret = client.get(f"/api/v1/repositories/{repo_id}/files/content", params={"path": ".env"})
    assert secret.status_code == 409 and secret.json()["error"]["code"] == "file_excluded"
    missing = client.get(
        f"/api/v1/repositories/{repo_id}/files/content", params={"path": "nope.py"}
    )
    assert missing.status_code == 404


def test_reindex_new_ref_and_force(client: TestClient, session_factory, enqueued) -> None:
    repo_id = client.post("/api/v1/repositories", json={"url": "owner/repo"}).json()["repository"][
        "id"
    ]
    _work(session_factory, enqueued)

    forced = client.post(f"/api/v1/repositories/{repo_id}/index", json={"force": True})
    assert forced.status_code == 202 and forced.json()["snapshot"]["commit_sha"] == SHA
    other = client.post(f"/api/v1/repositories/{repo_id}/index", json={"ref": "v2"})
    assert other.status_code == 202 and other.json()["snapshot"]["commit_sha"] == SHA2
    assert len(enqueued) == 2


def test_cancel_queued_job(client: TestClient, enqueued) -> None:
    job_id = client.post("/api/v1/repositories", json={"url": "owner/repo"}).json()["job"]["id"]
    r = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert r.status_code == 202 and r.json()["status"] == "cancelled"
    again = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert again.status_code == 409 and again.json()["error"]["code"] == "job_not_cancellable"
    # A new import after cancelling queues a fresh job.
    retry = client.post("/api/v1/repositories", json={"url": "owner/repo"})
    assert retry.status_code == 202 and retry.json()["job"]["id"] != job_id


def test_errors_use_one_shape(client: TestClient) -> None:
    bad = client.post("/api/v1/repositories", json={"url": "https://evil.example/x/y"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_repository_url"
    assert bad.json()["error"]["request_id"] == bad.headers["x-request-id"]

    private = client.post("/api/v1/repositories", json={"url": "owner/private"})
    assert private.status_code == 403
    assert private.json()["error"]["code"] == "repository_private"

    invalid = client.post("/api/v1/repositories", json={})
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "validation_error"


def test_other_workspace_is_invisible(client: TestClient, db: Session) -> None:
    other = Workspace(name="Other")
    db.add(other)
    db.flush()
    foreign = Repository(workspace_id=other.id, owner="x", name="y", default_branch="main")
    db.add(foreign)
    db.commit()

    assert client.get(f"/api/v1/repositories/{foreign.id}").status_code == 404
    assert client.get(f"/api/v1/repositories/{foreign.id}/files").status_code == 404
    assert client.post(f"/api/v1/repositories/{foreign.id}/index").status_code == 404
    assert client.get("/api/v1/repositories").json()["items"] == []
