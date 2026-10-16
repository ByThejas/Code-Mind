"""RQ entry points. Keep these thin: the work lives in pipeline.IndexRun."""

import uuid

from rq import get_current_job

from codemind.core.config import settings
from codemind.db.session import get_session_factory
from codemind.indexing.pipeline import IndexRun
from codemind.repositories.github import GitHubClient


def index_snapshot(job_id: str) -> None:
    rq_job = get_current_job()
    final_attempt = rq_job is None or not rq_job.retries_left
    IndexRun(
        uuid.UUID(job_id),
        session_factory=get_session_factory(),
        github_factory=lambda: GitHubClient(settings),
        settings=settings,
        final_attempt=final_attempt,
    ).run()
