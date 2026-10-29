"""Fixed-window request limits for the costly endpoints, per workspace (README "Rate limits")."""

import logging
import math
import time
import uuid
from collections.abc import Callable
from typing import Annotated, Literal, Protocol

from fastapi import Depends
from redis import Redis
from redis.exceptions import RedisError

from codemind.api.dependencies import get_current_workspace
from codemind.core.config import Settings, settings
from codemind.core.errors import CodemindError

log = logging.getLogger(__name__)

Bucket = Literal["model", "sandbox", "index"]
WINDOW_SECONDS = 60


class RateLimiter(Protocol):
    def hit(self, key: str, limit: int, window: int) -> int | None:
        """Count one request. Return None if allowed, else seconds until the window resets."""


class RedisRateLimiter:
    """One counter per key and window, expiring with the window. Fails open if Redis is down."""

    def __init__(self, redis: Redis, clock: Callable[[], float] = time.time) -> None:
        self.redis = redis
        self.clock = clock

    def hit(self, key: str, limit: int, window: int) -> int | None:
        now = self.clock()
        start = int(now // window) * window
        counter = f"codemind:ratelimit:{key}:{start}"
        try:
            pipe = self.redis.pipeline()
            pipe.incr(counter)
            pipe.expire(counter, window + 1)
            count = int(pipe.execute()[0])
        except RedisError:
            log.warning("rate limit check skipped: redis unavailable", exc_info=True)
            return None
        if count <= limit:
            return None
        return max(1, math.ceil(start + window - now))


_limiter: RedisRateLimiter | None = None


def get_rate_limiter() -> RateLimiter | None:
    """The shared limiter, or None when limits are turned off."""
    global _limiter
    if not settings.rate_limit_enabled:
        return None
    if _limiter is None:
        redis = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
        _limiter = RedisRateLimiter(redis)
    return _limiter


def limit_for(bucket: Bucket, config: Settings) -> int:
    limits: dict[Bucket, int] = {
        "model": config.rate_limit_model_per_minute,
        "sandbox": config.rate_limit_sandbox_per_minute,
        "index": config.rate_limit_index_per_minute,
    }
    return limits[bucket]


def rate_limit(bucket: Bucket) -> Callable[..., None]:
    """A route dependency that answers 429 once a workspace has used up `bucket` this minute."""

    def check(
        workspace_id: Annotated[uuid.UUID, Depends(get_current_workspace)],
        limiter: Annotated[RateLimiter | None, Depends(get_rate_limiter)],
    ) -> None:
        limit = limit_for(bucket, settings)
        if limiter is None or limit <= 0:
            return
        retry_after = limiter.hit(f"{workspace_id}:{bucket}", limit, WINDOW_SECONDS)
        if retry_after is not None:
            raise CodemindError(
                "rate_limited",
                f"Too many requests: at most {limit} per minute. Try again in {retry_after} s.",
                status_code=429,
                retryable=True,
                headers={"Retry-After": str(retry_after)},
            )

    return check
