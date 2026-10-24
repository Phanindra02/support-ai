"""The job queue, in Redis. The API puts slow work on it, and the worker (worker/main.py) runs it.

RQ keeps each job in Redis: the function to run, its arguments, and afterwards its result or
its error. Work that calls OpenAI or reads S3 goes here, so no request waits for it.
"""

from redis import Redis
from rq import Queue

from app.config import settings


def make_queue(url: str) -> Queue:
    """The queue on the Redis at url. The tests make one on their own Redis database."""
    # Not app.cache's client: RQ stores jobs as pickled bytes, which that client's
    # decode_responses would try to read as text. The worker waits on Redis for minutes at a
    # time for the next job, and RQ raises this client's timeout to allow for that.
    return Queue(connection=Redis.from_url(url))


queue = make_queue(settings.redis_url)
