"""Exits 0 if this container's worker is alive, and 1 if not: python -m worker.health.

The worker answers no HTTP, so unlike the API and the model service it can't have a /health
endpoint. Kubernetes and compose run this command in its container instead (Stage 8).

A running RQ worker keeps a key in Redis that expires unless the worker keeps renewing it,
between jobs and while one runs. A worker that's stuck stops renewing it, and drops out of
the list within about 8 minutes.
"""

import socket
import sys

from redis import Redis
from redis.exceptions import RedisError
from rq import Worker

from app.queue import queue


def is_alive(connection: Redis, hostname: str) -> bool:
    """Whether a worker on this host is in Redis's list of workers."""
    # Every container has its own hostname: on Kubernetes, the pod's name.
    return any(worker.hostname == hostname for worker in Worker.all(connection=connection))


def main() -> int:
    try:
        alive = is_alive(queue.connection, socket.gethostname())
    except RedisError as error:
        print(f"Can't reach Redis: {error}", file=sys.stderr)
        return 1
    if not alive:
        print("No worker on this host is in Redis's list of workers", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
