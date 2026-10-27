from functools import cache

from redis import Redis
from rq import Queue, Retry

from codemind.core.config import settings

RETRY_INTERVALS = [10, 60, 300]


@cache
def get_queue() -> Queue:
    return Queue(settings.indexing_queue, connection=Redis.from_url(settings.redis_url))


def enqueue_index_job(job_id: str) -> None:
    get_queue().enqueue(
        "codemind.indexing.tasks.index_snapshot",
        job_id,
        job_id=job_id,
        job_timeout=settings.max_job_seconds + 60,  # backstop; the pipeline enforces its own limit
        retry=Retry(max=len(RETRY_INTERVALS), interval=RETRY_INTERVALS),
        failure_ttl=7 * 24 * 3600,  # failed registry is the dead-letter queue
    )


@cache
def get_sandbox_queue() -> Queue:
    return Queue(settings.sandbox_queue, connection=Redis.from_url(settings.redis_url))


def enqueue_test_run(run_id: str) -> None:
    get_sandbox_queue().enqueue(
        "codemind.sandbox.tasks.run_tests",
        run_id,
        job_id=f"test-run-{run_id}",
        job_timeout=settings.sandbox_timeout_seconds + 120,  # the runner stops it first
        failure_ttl=7 * 24 * 3600,
    )


def cancel_queued_job(job_id: str) -> None:
    from rq.exceptions import NoSuchJobError
    from rq.job import Job as RQJob

    try:
        RQJob.fetch(job_id, connection=get_queue().connection).cancel()
    except NoSuchJobError:
        pass
