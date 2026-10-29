"""Phase 8 hardening: one error shape everywhere, request ids, readiness and input edge cases."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text
from sqlalchemy.orm import Session

from codemind.api.v1.health import get_probe_redis, migration_heads
from codemind.main import app
from tests.test_retrieval import _index


class FakeRedis:
    def __init__(self, up: bool = True) -> None:
        self.up = up

    def ping(self) -> bool:
        if not self.up:
            raise RedisConnectionError("down")
        return True


@pytest.fixture
def redis_up() -> Iterator[FakeRedis]:
    fake = FakeRedis()
    app.dependency_overrides[get_probe_redis] = lambda: fake
    yield fake


def test_unknown_route_uses_the_error_shape(client: TestClient) -> None:
    r = client.get("/api/v1/nope")
    assert r.status_code == 404
    error = r.json()["error"]
    assert error["code"] == "not_found"
    assert error["request_id"] == r.headers["x-request-id"]


def test_wrong_method_uses_the_error_shape_and_keeps_allow(client: TestClient) -> None:
    r = client.put("/health")
    assert r.status_code == 405
    assert r.json()["error"]["code"] == "method_not_allowed"
    assert "GET" in r.headers["allow"]


def test_a_well_formed_request_id_is_echoed(client: TestClient) -> None:
    r = client.get("/health", headers={"X-Request-ID": "trace-1234abcd"})
    assert r.headers["x-request-id"] == "trace-1234abcd"
    bad = client.get("/api/v1/nope", headers={"X-Request-ID": "<script>alert(1)</script>"})
    assert bad.headers["x-request-id"] != "<script>alert(1)</script>"
    assert bad.json()["error"]["request_id"] == bad.headers["x-request-id"]


def test_ready_when_everything_answers(client: TestClient, redis_up: FakeRedis) -> None:
    r = client.get("/health/ready")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "status": "ready",
        "checks": {"database": "ok", "migrations": "ok", "redis": "ok"},
    }


def test_not_ready_when_redis_is_down(client: TestClient, redis_up: FakeRedis) -> None:
    redis_up.up = False
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "not_ready" and r.json()["checks"]["redis"] == "unavailable"


def test_not_ready_when_migrations_are_behind(
    client: TestClient, redis_up: FakeRedis, db: Session
) -> None:
    (head,) = migration_heads()
    db.execute(text("UPDATE alembic_version SET version_num = '0001'"))
    db.commit()
    try:
        r = client.get("/health/ready")
        assert r.status_code == 503 and r.json()["checks"]["migrations"] == "behind"
    finally:
        db.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": head})
        db.commit()


def test_unknown_repository_cursor_is_rejected(client: TestClient) -> None:
    r = client.get(
        "/api/v1/repositories", params={"cursor": "00000000-0000-0000-0000-00000000abcd"}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_cursor"


def test_blank_inputs_are_rejected(client, session_factory, enqueued) -> None:
    repo_id = _index(client, session_factory, enqueued)
    question = client.post(
        f"/api/v1/repositories/{repo_id}/investigations", json={"question": "     "}
    )
    assert question.status_code == 422 and question.json()["error"]["code"] == "validation_error"
    search = client.get(f"/api/v1/repositories/{repo_id}/search", params={"q": "   "})
    assert search.status_code == 422 and search.json()["error"]["code"] == "invalid_query"
