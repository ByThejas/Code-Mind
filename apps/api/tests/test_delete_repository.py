"""Deleting a repository removes everything indexed from it and never races a running job."""

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from codemind.api.v1.routes import repositories as routes
from codemind.db.models import (
    Chunk,
    File,
    FileContent,
    GraphEdge,
    Investigation,
    Job,
    Repository,
    RepositorySnapshot,
    Symbol,
    Workspace,
)
from tests.graph_fixtures import GRAPH_FILES
from tests.test_api import _work
from tests.test_recovery import STALE, _mark_running

TABLES = (Repository, RepositorySnapshot, File, FileContent, Chunk, Symbol, GraphEdge, Job)


@pytest.fixture
def repo_files() -> dict[str, bytes]:
    return GRAPH_FILES


@pytest.fixture
def dropped(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Job ids removed from the queue, instead of talking to Redis."""
    calls: list[str] = []
    monkeypatch.setattr(routes, "cancel_queued_job", calls.append)
    return calls


def _counts(db: Session) -> dict[str, int]:
    return {t.__name__: db.scalar(select(func.count()).select_from(t)) or 0 for t in TABLES}


def _import(client: TestClient) -> dict:
    response = client.post("/api/v1/repositories", json={"url": "owner/repo"})
    assert response.status_code == 202, response.text
    return response.json()


def test_delete_removes_everything_indexed(
    client: TestClient, session_factory, enqueued, db: Session, dropped: list[str]
) -> None:
    repo_id = _import(client)["repository"]["id"]
    _work(session_factory, enqueued)
    snapshot = db.scalar(select(RepositorySnapshot))
    db.add(
        Investigation(
            workspace_id=snapshot.repository.workspace_id,
            repository_id=snapshot.repository_id,
            snapshot_id=snapshot.id,
            question="What does charge do?",
            status="succeeded",
        )
    )
    db.commit()
    before = _counts(db)
    assert all(before[t.__name__] > 0 for t in TABLES)

    response = client.delete(f"/api/v1/repositories/{repo_id}")
    assert response.status_code == 204 and response.content == b""
    db.expire_all()
    assert set(_counts(db).values()) == {0}
    assert db.scalar(select(func.count()).select_from(Investigation)) == 0
    assert dropped == []  # nothing was queued

    assert client.get(f"/api/v1/repositories/{repo_id}").status_code == 404
    again = client.delete(f"/api/v1/repositories/{repo_id}")
    assert again.status_code == 404
    assert again.json()["error"]["code"] == "repository_not_found"
    assert _import(client)["repository"]["id"] != repo_id  # it can be imported again


def test_delete_drops_queued_jobs(
    client: TestClient, session_factory, enqueued, db: Session, dropped: list[str]
) -> None:
    body = _import(client)
    response = client.delete(f"/api/v1/repositories/{body['repository']['id']}")
    assert response.status_code == 204
    assert dropped == [body["job"]["id"]]
    _work(session_factory, enqueued)  # a worker that still picks the job up does nothing
    assert set(_counts(db).values()) == {0}


def test_delete_waits_for_a_running_job(
    client: TestClient, session_factory, enqueued, db: Session, dropped: list[str]
) -> None:
    body = _import(client)
    repo_id, job_id = body["repository"]["id"], uuid.UUID(body["job"]["id"])
    _mark_running(db, job_id, silent_for=timedelta(0))

    busy = client.delete(f"/api/v1/repositories/{repo_id}")
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "repository_busy"
    assert str(job_id) in busy.json()["error"]["message"]
    assert client.get(f"/api/v1/repositories/{repo_id}").status_code == 200

    # Once the worker has gone quiet for too long, the job is failed and deletion goes ahead.
    _mark_running(db, job_id, silent_for=STALE)
    assert client.delete(f"/api/v1/repositories/{repo_id}").status_code == 204
    db.expire_all()
    assert db.get(Job, job_id) is None


def test_cannot_delete_another_workspaces_repository(
    client: TestClient, db: Session, dropped: list[str]
) -> None:
    other = Workspace(name="Other")
    db.add(other)
    db.flush()
    foreign = Repository(workspace_id=other.id, owner="x", name="y", default_branch="main")
    db.add(foreign)
    db.commit()
    assert client.delete(f"/api/v1/repositories/{foreign.id}").status_code == 404
    db.expire_all()
    assert db.get(Repository, foreign.id) is not None
