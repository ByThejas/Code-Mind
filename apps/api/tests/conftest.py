import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import respx
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from codemind.api.dependencies import get_db, get_enqueue, get_github
from codemind.core.config import Settings
from codemind.main import app
from codemind.repositories.github import GitHubClient
from tests.fixtures import SHA, SHA2, make_tarball

API_ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://codemind:local_password_change_me@localhost:5432/codemind_test",
)
TEST_SETTINGS = Settings(max_file_bytes=5000, max_repository_files=100)
TABLES = (
    "jobs, embeddings, chunks, symbols, file_contents, files, repository_snapshots, repositories"
)


@pytest.fixture(scope="session")
def migrated_engine():
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect():
            pass
    except OperationalError:
        if os.environ.get("CI"):
            raise
        pytest.skip("Postgres is not reachable; set TEST_DATABASE_URL to run database tests")
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(migrated_engine) -> Iterator[sessionmaker[Session]]:
    with migrated_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} CASCADE"))
        conn.execute(
            text("DELETE FROM workspaces WHERE id <> '00000000-0000-0000-0000-000000000001'")
        )
    yield sessionmaker(bind=migrated_engine, expire_on_commit=False)


@pytest.fixture
def db(session_factory) -> Iterator[Session]:
    with session_factory() as session:
        yield session


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
        with GitHubClient(TEST_SETTINGS) as gh:
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
