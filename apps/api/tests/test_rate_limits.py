"""Per-workspace request limits on the costly endpoints."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from codemind.api.ratelimit import RedisRateLimiter, get_rate_limiter
from codemind.core.config import settings
from codemind.main import app


class FakePipeline:
    def __init__(self, redis: "FakeRedis") -> None:
        self.redis = redis
        self.ops: list[tuple[str, str]] = []

    def incr(self, key: str) -> None:
        self.ops.append(("incr", key))

    def expire(self, key: str, seconds: int) -> None:
        self.ops.append(("expire", key))

    def execute(self) -> list[Any]:
        if not self.redis.up:
            raise RedisConnectionError("down")
        results: list[Any] = []
        for op, key in self.ops:
            if op == "incr":
                self.redis.counts[key] = self.redis.counts.get(key, 0) + 1
                results.append(self.redis.counts[key])
            else:
                results.append(True)
        return results


class FakeRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.up = True

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_limit_allows_up_to_the_limit_then_says_when_to_retry() -> None:
    clock = Clock(1_000_040.0)  # 20 s into a minute window
    limiter = RedisRateLimiter(FakeRedis(), clock=clock)  # type: ignore[arg-type]
    assert [limiter.hit("w:model", 2, 60) for _ in range(2)] == [None, None]
    assert limiter.hit("w:model", 2, 60) == 40
    assert limiter.hit("w:sandbox", 2, 60) is None  # buckets are separate
    clock.now += 40  # next window
    assert limiter.hit("w:model", 2, 60) is None


def test_limit_fails_open_when_redis_is_down() -> None:
    redis = FakeRedis()
    redis.up = False
    limiter = RedisRateLimiter(redis)  # type: ignore[arg-type]
    assert all(limiter.hit("w:model", 1, 60) is None for _ in range(3))


@pytest.fixture
def limited(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    limiter = RedisRateLimiter(FakeRedis())  # type: ignore[arg-type]
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    monkeypatch.setattr(settings, "rate_limit_index_per_minute", 2)
    return client


def test_too_many_imports_get_429_with_retry_after(limited: TestClient) -> None:
    for _ in range(2):
        assert limited.post("/api/v1/repositories", json={"url": "owner/repo"}).status_code in (
            200,
            202,
        )
    r = limited.post("/api/v1/repositories", json={"url": "owner/repo"})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(r.headers["retry-after"]) <= 60
    # Reads are never limited.
    assert limited.get("/api/v1/repositories").status_code == 200


def test_a_zero_limit_turns_it_off(limited: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_index_per_minute", 0)
    for _ in range(5):
        assert limited.post("/api/v1/repositories", json={"url": "owner/repo"}).status_code != 429
