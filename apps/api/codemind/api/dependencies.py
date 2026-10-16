import uuid
from collections.abc import Iterator

from codemind.core.config import settings
from codemind.db.models import DEFAULT_WORKSPACE_ID
from codemind.db.session import get_db
from codemind.indexing.queue import enqueue_index_job
from codemind.repositories.github import GitHubClient
from codemind.repositories.service import Enqueue

__all__ = ["get_current_workspace", "get_db", "get_enqueue", "get_github"]


def get_current_workspace() -> uuid.UUID:
    """The caller's workspace. A single seeded workspace until authentication lands."""
    return DEFAULT_WORKSPACE_ID


def get_github() -> Iterator[GitHubClient]:
    with GitHubClient(settings) as client:
        yield client


def get_enqueue() -> Enqueue:
    return enqueue_index_job
