"""Rate limiting: each IP address may make RATE_LIMIT_PER_MINUTE requests in any 60 seconds.

The algorithm is a sliding window log. Each IP has a Redis sorted set holding the time of each
of its requests in the last 60 seconds. A new request removes times older than that, adds its
own, and counts. Over the limit, it gets a 429. Unlike a counter that resets on the minute,
this can't be gamed by sending a full minute's worth just before the reset and again just
after it. It costs one stored time per allowed request, at most 60 per IP.
"""

import logging
import math
import time
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, HTTPException, Request
from redis import Redis, RedisError

from app.cache import get_redis
from app.config import settings

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60


def seconds_until_allowed(redis: Redis, key: str, limit: int, now: float) -> float:
    """Record a request in the log at `key`. Returns 0 if it's allowed.

    Over the limit, the request is taken back out of the log, so rejected requests don't use
    up the limit, and the return value is how many seconds until the oldest request in the
    window drops out and frees a place.
    """
    # Unique even when two requests arrive at the same instant.
    request_id = f"{now}-{uuid4().hex}"
    # transaction=True wraps the commands in MULTI and EXEC, so they run as one step. Two
    # requests at once can't both see 59 and both get in as the 60th.
    with redis.pipeline(transaction=True) as pipe:
        pipe.zremrangebyscore(key, "-inf", now - WINDOW_SECONDS)  # Forget requests too old.
        pipe.zadd(key, {request_id: now})
        pipe.zcard(key)
        pipe.zrange(key, 0, 0, withscores=True)  # The oldest request still in the window.
        pipe.expire(key, WINDOW_SECONDS)  # The log of an IP that goes quiet deletes itself.
        _, _, count, [(_, oldest)], _ = pipe.execute()

    if count <= limit:
        return 0
    redis.zrem(key, request_id)
    return oldest + WINDOW_SECONDS - now


def rate_limit(request: Request, redis: Annotated[Redis, Depends(get_redis)]) -> None:
    """A FastAPI dependency that stops a request with a 429 if its IP is over the limit."""
    # The address the request came from. Behind a proxy (Stage 5), this is the proxy's
    # address unless uvicorn is told to trust the proxy's X-Forwarded-For header.
    ip = request.client.host if request.client else "unknown"
    try:
        wait = seconds_until_allowed(
            redis, f"ratelimit:{ip}", settings.rate_limit_per_minute, time.time()
        )
    except RedisError as error:
        # Let the request through. Without Redis there's no limit, but the API stays up.
        logger.warning("Skipped the rate limit, because Redis failed: %s", error)
        return

    if wait > 0:
        seconds = max(1, math.ceil(wait))
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests. Try again in {seconds} seconds.",
            headers={"Retry-After": str(seconds)},
        )
