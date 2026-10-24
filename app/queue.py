"""The job queue, in Redis. The API puts slow work on it, and the worker (worker/main.py) runs it.

RQ keeps each job in Redis: the function to run, its arguments, and afterwards its result or
its error. Work that calls OpenAI or reads S3 goes here, so no request waits for it.
"""

from typing import Annotated

from fastapi import Depends
from redis import Redis
from rq import Queue

from app.config import settings

# The job that splits a processed document into chunks and embeds them (worker/jobs.py). It's
# queued by its import path, so the API doesn't import the worker's code to queue it.
EMBED_DOCUMENT = "worker.jobs.embed_document"


def make_queue(url: str) -> Queue:
    """The queue on the Redis at url. The tests make one on their own Redis database."""
    # Not app.cache's client: RQ stores jobs as pickled bytes, which that client's
    # decode_responses would try to read as text. The worker waits on Redis for minutes at a
    # time for the next job, and RQ raises this client's timeout to allow for that.
    return Queue(connection=Redis.from_url(url))


queue = make_queue(settings.redis_url)


def get_queue() -> Queue:
    """The queue, as a FastAPI dependency, so tests can swap in one on their own Redis."""
    return queue


QueueDep = Annotated[Queue, Depends(get_queue)]
