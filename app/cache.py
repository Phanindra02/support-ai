"""A Redis cache for results that are slow to work out and asked for often.

The cache is a speed-up, not a source of truth. If Redis is slow or down, these helpers log a
warning and carry on, and the app answers from Postgres instead of failing.
"""

import logging
from typing import Annotated

from fastapi import Depends
from redis import Redis, RedisError
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.config import settings

logger = logging.getLogger(__name__)


def make_redis(url: str) -> Redis:
    """Make a Redis client that gives up quickly. The tests use it too."""
    return Redis.from_url(
        url,
        decode_responses=True,  # Return str instead of bytes.
        # If Redis stops answering, a request waits half a second at most, then carries on
        # without the cache.
        socket_timeout=0.5,
        socket_connect_timeout=0.5,
        # No retries. By default redis-py retries a failed command 3 times with growing
        # waits, which made each cache call take 2.7 seconds while Redis was down.
        retry=Retry(NoBackoff(), 0),
    )


redis_client = make_redis(settings.redis_url)


def get_redis() -> Redis:
    """The Redis client, as a FastAPI dependency, so tests can swap in their own."""
    return redis_client


RedisDep = Annotated[Redis, Depends(get_redis)]


def read_cache(redis: Redis, key: str) -> str | None:
    """The cached value, or None if there isn't one or Redis can't be reached."""
    try:
        value = redis.get(key)
        return value.decode() if isinstance(value, bytes) else value
    except RedisError as error:
        logger.warning("Skipped reading the cache, because Redis failed: %s", error)
        return None


def clear_cache(redis: Redis, key: str) -> None:
    """Delete a cached value, so the next read works it out again from the database."""
    try:
        redis.delete(key)
    except RedisError as error:
        # The value goes stale, but only until it expires by itself.
        logger.warning("Skipped clearing the cache, because Redis failed: %s", error)


def write_cache(redis: Redis, key: str, value: str, seconds: int) -> None:
    """Cache a value. Redis deletes it by itself after `seconds`."""
    try:
        redis.set(key, value, ex=seconds)
    except RedisError as error:
        logger.warning("Skipped writing the cache, because Redis failed: %s", error)
