"""Indexing worker: `python worker.py` (from apps/api) runs one RQ worker on the indexing queue."""

import logging

from rq import Worker

from codemind.core.config import settings
from codemind.db.session import get_session_factory
from codemind.indexing.queue import get_queue
from codemind.indexing.recovery import reap_stale_jobs

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with get_session_factory()() as db:  # jobs whose worker died while no request touched them
        reap_stale_jobs(db, settings)
        db.commit()
    queue = get_queue()
    Worker([queue], connection=queue.connection, name=None).work(with_scheduler=True)
