"""Worker: `python worker.py` (from apps/api) runs one RQ worker for indexing and test runs."""

import logging
import sys

from rq import SimpleWorker, Worker

from codemind.core.config import settings
from codemind.db.session import get_session_factory
from codemind.indexing.queue import get_queue, get_sandbox_queue
from codemind.indexing.recovery import reap_stale_jobs

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with get_session_factory()() as db:  # jobs whose worker died while no request touched them
        reap_stale_jobs(db, settings)
        db.commit()
    queue = get_queue()
    # Windows has no os.fork(), so run each job in this process there instead of a forked child.
    worker_class = SimpleWorker if sys.platform == "win32" else Worker
    # Queues are served in order: indexing jobs are taken before test runs.
    worker_class([queue, get_sandbox_queue()], connection=queue.connection, name=None).work(
        with_scheduler=True
    )
