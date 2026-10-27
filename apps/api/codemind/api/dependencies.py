import uuid
from collections.abc import Iterator

from codemind.core.config import settings
from codemind.db.models import DEFAULT_WORKSPACE_ID
from codemind.db.session import get_db
from codemind.indexing.queue import enqueue_index_job, enqueue_test_run
from codemind.providers import AnswerModel, EmbeddingProvider
from codemind.providers import get_answer_model as _answer_model
from codemind.providers import get_embedding_provider as _embedding_provider
from codemind.repositories.github import GitHubClient
from codemind.repositories.service import Enqueue

__all__ = [
    "get_answer_model",
    "get_current_workspace",
    "get_db",
    "get_embedding_provider",
    "get_enqueue",
    "get_enqueue_test_run",
    "get_github",
]


def get_current_workspace() -> uuid.UUID:
    """The caller's workspace. A single seeded workspace until authentication lands."""
    return DEFAULT_WORKSPACE_ID


def get_github() -> Iterator[GitHubClient]:
    with GitHubClient(settings) as client:
        yield client


def get_enqueue() -> Enqueue:
    return enqueue_index_job


def get_embedding_provider() -> EmbeddingProvider | None:
    return _embedding_provider(settings)


def get_answer_model() -> AnswerModel | None:
    return _answer_model(settings)


def get_enqueue_test_run() -> Enqueue:
    return enqueue_test_run
