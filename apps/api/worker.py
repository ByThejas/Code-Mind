"""Indexing worker: `python worker.py` (from apps/api) runs one RQ worker on the indexing queue."""

import logging

from rq import Worker

from codemind.indexing.queue import get_queue

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    queue = get_queue()
    Worker([queue], connection=queue.connection, name=None).work(with_scheduler=True)
