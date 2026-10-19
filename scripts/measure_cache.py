"""Measure how much the Redis cache speeds up GET /stats.

Start the API in another terminal first, with the rate limit raised so it doesn't stop these
400 requests, then run this from the project root:

    RATE_LIMIT_PER_MINUTE=100000 uv run uvicorn app.main:app
    uv run python -m scripts.measure_cache                              # http://127.0.0.1:8000
    uv run python -m scripts.measure_cache --url http://127.0.0.1:9000

A "miss" request runs right after the cached counts are deleted, so the API counts tickets in
Postgres. A "hit" request finds the counts in Redis. Times are measured from this script, so
they include the HTTP round trip on this machine.

/stats needs a logged-in agent. This script reads the same .env as the API, so it signs a
token for the first active agent itself, instead of asking for a password.
"""

import argparse
import time
from statistics import median, quantiles

import httpx
from redis import Redis
from sqlalchemy import select

from app.cache import make_redis
from app.config import settings
from app.database import SessionLocal
from app.models import Agent
from app.routers.tickets import STATS_KEY
from app.security import create_access_token

REQUESTS = 200


def time_stats(client: httpx.Client, redis: Redis, expected: str) -> list[float]:
    """Time GET /stats REQUESTS times, in milliseconds. expected is "HIT" or "MISS"."""
    times = []
    for _ in range(REQUESTS):
        if expected == "MISS":
            redis.delete(STATS_KEY)
        start = time.perf_counter()
        response = client.get("/stats")
        times.append((time.perf_counter() - start) * 1000)
        if response.status_code == 429:
            raise SystemExit("Rate limited. Start the API with RATE_LIMIT_PER_MINUTE=100000.")
        response.raise_for_status()
        if response.headers["X-Cache"] != expected:
            raise SystemExit(f"Expected a cache {expected}, got {response.headers['X-Cache']}.")
    return times


def agent_token() -> str:
    """A login token for the first active agent."""
    with SessionLocal() as db:
        agent = db.scalar(select(Agent).where(Agent.is_active).order_by(Agent.id).limit(1))
        if agent is None:
            raise SystemExit("There are no active agents. Run the seed script first.")
        return create_access_token(agent)


def p95(times: list[float]) -> float:
    """The time 95% of requests beat."""
    return quantiles(times, n=20)[-1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Time GET /stats with and without the cache.")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="where the API runs")
    args = parser.parse_args()

    redis = make_redis(settings.redis_url)
    headers = {"Authorization": f"Bearer {agent_token()}"}
    with httpx.Client(base_url=args.url, headers=headers) as client:
        client.get("/stats")  # Warm-up: opens the connection and loads the tables into memory.
        misses = time_stats(client, redis, "MISS")
        hits = time_stats(client, redis, "HIT")

    print(f"GET /stats, {REQUESTS} requests each\n")
    print(f"{'':16} {'median ms':>10} {'p95 ms':>8}")
    for name, times in [("miss (Postgres)", misses), ("hit (Redis)", hits)]:
        print(f"{name:16} {median(times):>10.2f} {p95(times):>8.2f}")
    print(f"\nA hit is {median(misses) / median(hits):.1f}x faster at the median.")


if __name__ == "__main__":
    main()
