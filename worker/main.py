"""The background worker. It waits for jobs on the queue in Redis and runs them one at a time.

On the Mac: uv run python -m worker.main. In docker compose, it's the worker service.
"""

from rq import Worker

from app.queue import queue


def main() -> None:
    # For each job, RQ's Worker forks a copy of itself to run it, so a job that crashes or
    # leaks memory can't take the worker down with it. On a stop signal, it finishes the job
    # it's running before it exits. The scheduler puts a failed job back on the queue when
    # its wait before the next try is over (app/queue.py).
    Worker([queue], connection=queue.connection).work(with_scheduler=True)


if __name__ == "__main__":
    main()
