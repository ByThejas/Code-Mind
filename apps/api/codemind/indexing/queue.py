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


def cancel_queued_job(job_id: str) -> None:
    from rq.exceptions import NoSuchJobError
    from rq.job import Job as RQJob

    try:
        RQJob.fetch(job_id, connection=get_queue().connection).cancel()
    except NoSuchJobError:
        pass
