"""RQ entry point for test runs. The work lives in sandbox.runs."""

import uuid

from codemind.core.config import settings
from codemind.db.session import get_session_factory
from codemind.sandbox.docker import DockerRunner
from codemind.sandbox.runs import execute_test_run


def run_tests(run_id: str) -> None:
    execute_test_run(
        uuid.UUID(run_id), get_session_factory(), DockerRunner(settings.sandbox_docker), settings
    )
