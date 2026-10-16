import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

API_ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://codemind:local_password_change_me@localhost:5432/codemind_test",
)
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
