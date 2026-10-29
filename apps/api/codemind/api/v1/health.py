"""Liveness (`/health`) and readiness (`/health/ready`) probes."""

import logging
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Annotated, Literal

from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from codemind.core.config import settings
from codemind.db.session import get_db

log = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])

MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"
PROBE_TIMEOUT_SECONDS = 2.0

Check = Literal["ok", "unavailable", "behind", "unknown"]


class Readiness(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, Check]


def get_probe_redis() -> Iterator[Redis]:
    client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=PROBE_TIMEOUT_SECONDS,
        socket_timeout=PROBE_TIMEOUT_SECONDS,
    )
    try:
        yield client
    finally:
        client.close()


@cache
def migration_heads() -> frozenset[str]:
    return frozenset(ScriptDirectory(str(MIGRATIONS)).get_heads())


@router.get("/health")
def health() -> dict[str, str]:
    """Check whether the API process is responding."""
    return {"status": "healthy"}


@router.get(
    "/health/ready",
    response_model=Readiness,
    responses={503: {"model": Readiness, "description": "A dependency is down or out of date."}},
)
def ready(
    db: Annotated[Session, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_probe_redis)],
) -> JSONResponse:
    """Check that Postgres and Redis answer and the database schema is at the latest migration."""
    checks: dict[str, Check] = {}
    try:
        db.execute(text(f"SET LOCAL statement_timeout = {int(PROBE_TIMEOUT_SECONDS * 1000)}"))
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
        applied = set(db.scalars(text("SELECT version_num FROM alembic_version")))
        checks["migrations"] = "ok" if applied == migration_heads() else "behind"
    except SQLAlchemyError:
        log.warning("readiness: database check failed", exc_info=True)
        checks.setdefault("database", "unavailable")
        checks.setdefault("migrations", "unknown" if checks["database"] != "ok" else "behind")
    finally:
        db.rollback()
    try:
        redis.ping()
        checks["redis"] = "ok"
    except RedisError:
        log.warning("readiness: redis check failed", exc_info=True)
        checks["redis"] = "unavailable"
    ok = all(v == "ok" for v in checks.values())
    body = Readiness(status="ready" if ok else "not_ready", checks=checks)
    return JSONResponse(body.model_dump(), status_code=200 if ok else 503)
