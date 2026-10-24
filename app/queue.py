"""The job queue, in Redis. The API puts slow work on it, and the worker (worker/main.py) runs it.

RQ keeps each job in Redis: the function to run, its arguments, and afterwards its result or
its error. Work that calls OpenAI or reads S3 goes here, so no request waits for it.
"""

import logging
from typing import Annotated

from fastapi import Depends
from redis import Redis
from redis.exceptions import RedisError
from rq import Queue, Retry

from app.config import settings

# The job that splits a processed document into chunks and embeds them (worker/jobs.py). It's
# queued by its import path, so the API doesn't import the worker's code to queue it.
EMBED_DOCUMENT = "worker.jobs.embed_document"

# A failed embedding job runs again up to 3 times, after 30 seconds, 2 minutes, then 10, so an
# OpenAI outage or rate limit gets time to pass. After that, RQ keeps it in its failed job
# registry, a separate list in Redis, with its error, until someone looks
# (scripts/failed_jobs.py). Running it again is safe: it replaces the document's chunks.
EMBED_RETRY = Retry(max=3, interval=[30, 120, 600])

# The job that asks the model service for a new ticket's priority (worker/jobs.py).
PREDICT_PRIORITY = "worker.jobs.predict_priority"

# Shorter waits than embedding: an agent may open the ticket within minutes, and the model
# service is only down while it restarts.
PREDICT_RETRY = Retry(max=3, interval=[5, 30, 120])

logger = logging.getLogger(__name__)


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


def queue_priority_prediction(queue: Queue, ticket_id: int) -> None:
    """Queue the priority prediction for a new ticket, if the model service is set up.

    A ticket is never turned away over this. If Redis is down, the ticket is still saved, at
    medium priority, and an agent can set it.
    """
    if not settings.model_service_url:
        return
    try:
        queue.enqueue(PREDICT_PRIORITY, ticket_id, retry=PREDICT_RETRY)
    except RedisError as error:
        logger.warning("Couldn't queue ticket %s for a priority prediction: %s", ticket_id, error)
