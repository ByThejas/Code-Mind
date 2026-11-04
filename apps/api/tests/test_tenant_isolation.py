"""Tenant isolation: one workspace can never read, change or point at another workspace's data.

Workspace A indexes a repository and creates an investigation, a patch and a test run. Workspace B
then calls every endpoint that takes an id with A's ids, and calls its own repository's endpoints
with A's snapshot and symbol ids. Each call must fail without echoing A's data, and A's data must
be unchanged afterwards.

`test_every_id_route_is_covered` fails when a new endpoint with an id in its path is added
without a case below, so isolation is checked for each new endpoint, not only for today's.
"""

import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from codemind.api.dependencies import get_current_workspace, get_enqueue_test_run
from codemind.db.models import DEFAULT_WORKSPACE_ID, Job, Repository, Workspace
from codemind.main import app
from tests.test_patches import GOOD_EDIT, FakeModel, providers  # noqa: F401
from tests.test_retrieval import HashEmbeddings, _index


@dataclass
class Tenant:
    workspace_id: uuid.UUID
    repository_id: str
    snapshot_id: str
    symbol_ids: list[str]


@dataclass
class World:
    a: Tenant
    b: Tenant
    job_id: str
    investigation_id: str
    patch_id: str
    diff_sha256: str
    test_run_id: str
    act_as: "ActAs"


class ActAs:
    """Switches the caller's workspace, the way authentication will once it lands."""

    def __init__(self) -> None:
        self.current = DEFAULT_WORKSPACE_ID
        app.dependency_overrides[get_current_workspace] = lambda: self.current

    def __call__(self, workspace_id: uuid.UUID) -> None:
        self.current = workspace_id


def _tenant(
    client: TestClient,
    session_factory: sessionmaker[Session],
    enqueued: list[str],
    workspace_id: uuid.UUID,
) -> Tenant:
    repo_id = _index(client, session_factory, enqueued, HashEmbeddings())
    symbols = client.get(f"/api/v1/repositories/{repo_id}/symbols")
    assert symbols.status_code == 200, symbols.text
    body = symbols.json()
    assert len(body["items"]) >= 2, "the fixture repository should have at least two symbols"
    return Tenant(
        workspace_id=workspace_id,
        repository_id=repo_id,
        snapshot_id=body["snapshot_id"],
        symbol_ids=[s["id"] for s in body["items"]],
    )


@pytest.fixture
def world(
    client: TestClient,
    session_factory: sessionmaker[Session],
    enqueued: list[str],
    providers: dict[str, object],  # noqa: F811
    db: Session,
) -> Iterator[World]:
    act_as = ActAs()
    app.dependency_overrides[get_enqueue_test_run] = lambda: lambda _id: None
    providers["embedder"] = HashEmbeddings()
    providers["model"] = FakeModel()

    a = _tenant(client, session_factory, enqueued, DEFAULT_WORKSPACE_ID)
    job_id = str(db.scalars(select(Job.id)).one())
    investigation = client.post(
        f"/api/v1/repositories/{a.repository_id}/investigations",
        json={"question": "How does greet work?"},
    )
    assert investigation.status_code == 201, investigation.text
    investigation_id = investigation.json()["id"]
    patch = client.post(
        f"/api/v1/investigations/{investigation_id}/patches",
        json={"instruction": "Say hi", "edits": [GOOD_EDIT]},
    )
    assert patch.status_code == 201, patch.text
    run = client.post(f"/api/v1/patches/{patch.json()['id']}/test-runs", json={})
    assert run.status_code == 202, run.text

    other = Workspace(name="Tenant B")
    db.add(other)
    db.commit()
    act_as(other.id)
    b = _tenant(client, session_factory, enqueued, other.id)

    yield World(
        a=a,
        b=b,
        job_id=job_id,
        investigation_id=investigation_id,
        patch_id=patch.json()["id"],
        diff_sha256=patch.json()["diff_sha256"],
        test_run_id=run.json()["id"],
        act_as=act_as,
    )


@dataclass
class Case:
    method: str
    route: str
    query: dict[str, object] | None = None
    body: dict[str, object] | None = None


def _foreign_id_cases(w: World) -> list[Case]:
    """Every endpoint with an id in its path, called by B with A's ids and an otherwise valid request."""
    sym = w.a.symbol_ids
    return [
        Case("GET", "/api/v1/repositories/{repository_id}"),
        Case("DELETE", "/api/v1/repositories/{repository_id}"),
        Case("POST", "/api/v1/repositories/{repository_id}/index", body={"force": True}),
        Case("GET", "/api/v1/jobs/{job_id}"),
        Case("POST", "/api/v1/jobs/{job_id}/cancel"),
        Case("GET", "/api/v1/repositories/{repository_id}/files"),
        Case("GET", "/api/v1/repositories/{repository_id}/files/content", {"path": "src/app.py"}),
        Case("GET", "/api/v1/repositories/{repository_id}/search", {"q": "greet"}),
        Case(
            "POST",
            "/api/v1/repositories/{repository_id}/investigations",
            body={"question": "How does greet work?"},
        ),
        Case("GET", "/api/v1/repositories/{repository_id}/investigations"),
        Case("GET", "/api/v1/investigations/{investigation_id}"),
        Case("GET", "/api/v1/repositories/{repository_id}/symbols"),
        Case("GET", "/api/v1/repositories/{repository_id}/symbols/{symbol_id}"),
        Case(
            "GET", "/api/v1/repositories/{repository_id}/graph/neighborhood", {"symbol_id": sym[0]}
        ),
        Case(
            "GET",
            "/api/v1/repositories/{repository_id}/graph/trace",
            {"from_symbol_id": sym[0], "to_symbol_id": sym[1]},
        ),
        Case("GET", "/api/v1/repositories/{repository_id}/graph/modules"),
        Case(
            "POST",
            "/api/v1/repositories/{repository_id}/impact-analysis",
            body={"targets": [{"path": "src/app.py"}]},
        ),
        Case(
            "POST",
            "/api/v1/investigations/{investigation_id}/patches",
            body={"instruction": "Say hi", "edits": [GOOD_EDIT]},
        ),
        Case("GET", "/api/v1/investigations/{investigation_id}/patches"),
        Case("GET", "/api/v1/patches/{patch_id}"),
        Case("POST", "/api/v1/patches/{patch_id}/approve", body={"diff_sha256": w.diff_sha256}),
        Case("POST", "/api/v1/patches/{patch_id}/reject", body={"reason": "not mine"}),
        Case("POST", "/api/v1/patches/{patch_id}/export"),
        Case("POST", "/api/v1/patches/{patch_id}/test-runs", body={}),
        Case("GET", "/api/v1/patches/{patch_id}/test-runs"),
        Case("GET", "/api/v1/test-runs/{test_run_id}"),
    ]


def _send_as_b(client: TestClient, w: World, case: Case):
    """Fills the route with workspace A's ids and sends it (the caller is workspace B)."""
    ids = {
        "repository_id": w.a.repository_id,
        "job_id": w.job_id,
        "investigation_id": w.investigation_id,
        "patch_id": w.patch_id,
        "test_run_id": w.test_run_id,
        "symbol_id": w.a.symbol_ids[0],
    }
    url = re.sub(r"\{(\w+)\}", lambda m: ids[m.group(1)], case.route)
    return client.request(case.method, url, params=case.query, json=case.body)


def _id_routes() -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, ops in app.openapi()["paths"].items()
        if "{" in path
        for method in ops
    }


def test_every_id_route_is_covered(world: World) -> None:
    covered = {(c.method, c.route) for c in _foreign_id_cases(world)}
    missing = sorted(_id_routes() - covered)
    assert not missing, f"add a cross-workspace case for: {missing}"


def test_another_workspaces_ids_are_not_found(client: TestClient, world: World) -> None:
    w = world
    secrets = {w.a.repository_id, w.a.snapshot_id, w.investigation_id, w.patch_id, w.test_run_id}
    for case in _foreign_id_cases(w):
        r = _send_as_b(client, w, case)
        assert r.status_code == 404, f"{case.method} {case.route} -> {r.status_code}: {r.text}"
        assert r.json()["error"]["code"].endswith("_not_found"), r.text
        leaked = [s for s in secrets if s in r.text]
        assert not leaked, f"{case.method} {case.route} echoed workspace A's ids: {leaked}"


def test_own_repository_cannot_reach_another_workspaces_snapshot_or_symbols(
    client: TestClient, world: World
) -> None:
    """B names its own repository but A's snapshot or symbols: nothing of A's comes back."""
    w = world
    base = f"/api/v1/repositories/{w.b.repository_id}"
    foreign_snapshot = {"snapshot_id": w.a.snapshot_id}
    snapshot_calls = [
        ("GET", f"{base}/files", foreign_snapshot, None),
        ("GET", f"{base}/files/content", foreign_snapshot | {"path": "src/app.py"}, None),
        ("GET", f"{base}/search", foreign_snapshot | {"q": "greet"}, None),
        ("GET", f"{base}/symbols", foreign_snapshot, None),
        ("GET", f"{base}/graph/modules", foreign_snapshot, None),
        ("POST", f"{base}/investigations", None, {"question": "greet?"} | foreign_snapshot),
        (
            "POST",
            f"{base}/impact-analysis",
            None,
            {"targets": [{"path": "src/app.py"}]} | foreign_snapshot,
        ),
    ]
    for method, url, query, body in snapshot_calls:
        r = client.request(method, url, params=query, json=body)
        assert r.status_code == 409, f"{method} {url} -> {r.status_code}: {r.text}"
        assert r.json()["error"]["code"] == "snapshot_not_ready", r.text

    a_sym, a_sym2 = w.a.symbol_ids[:2]
    symbol_calls = [
        f"{base}/symbols/{a_sym}",
        f"{base}/graph/neighborhood?symbol_id={a_sym}",
        f"{base}/graph/trace?from_symbol_id={a_sym}&to_symbol_id={a_sym2}",
        f"{base}/graph/trace?from_symbol_id={w.b.symbol_ids[0]}&to_symbol_id={a_sym}",
    ]
    for url in symbol_calls:
        r = client.get(url)
        assert r.status_code == 404, f"GET {url} -> {r.status_code}: {r.text}"
        assert r.json()["error"]["code"] == "symbol_not_found", r.text

    impact = client.post(f"{base}/impact-analysis", json={"targets": [{"symbol_id": a_sym}]})
    assert impact.status_code == 200, impact.text
    result = impact.json()
    assert [u["reason"] for u in result["unresolved"]] == ["symbol_not_found"]
    assert w.a.snapshot_id not in impact.text


def test_lists_only_show_the_callers_own_data(client: TestClient, world: World) -> None:
    w = world
    repos = client.get("/api/v1/repositories").json()["items"]
    assert [r["id"] for r in repos] == [w.b.repository_id]
    cursor = client.get("/api/v1/repositories", params={"cursor": w.a.repository_id})
    assert cursor.status_code == 422 and cursor.json()["error"]["code"] == "invalid_cursor"
    own = client.get(
        f"/api/v1/repositories/{w.b.repository_id}/investigations",
        params={"cursor": w.investigation_id},
    )
    assert own.status_code == 422 and own.json()["error"]["code"] == "invalid_cursor"


def test_workspace_a_is_untouched_after_the_attempts(
    client: TestClient, world: World, db: Session
) -> None:
    w = world
    for case in _foreign_id_cases(w):  # every attempt from B, including deletes and approvals
        _send_as_b(client, w, case)

    w.act_as(w.a.workspace_id)
    assert db.get(Repository, uuid.UUID(w.a.repository_id)) is not None
    repo = client.get(f"/api/v1/repositories/{w.a.repository_id}").json()
    assert repo["latest_snapshot"]["id"] == w.a.snapshot_id
    assert client.get(f"/api/v1/jobs/{w.job_id}").json()["status"] == "succeeded"
    patch = client.get(f"/api/v1/patches/{w.patch_id}").json()
    assert patch["status"] == "validated" and patch["diff_sha256"] == w.diff_sha256
    investigations = client.get(f"/api/v1/repositories/{w.a.repository_id}/investigations")
    assert [i["id"] for i in investigations.json()["items"]] == [w.investigation_id]
    runs = client.get(f"/api/v1/patches/{w.patch_id}/test-runs").json()["items"]
    assert [r["id"] for r in runs] == [w.test_run_id]
