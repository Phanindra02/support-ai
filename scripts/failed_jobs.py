"""List the jobs that failed after all their tries, with their errors, or run them again.

uv run python -m scripts.failed_jobs            # list them
uv run python -m scripts.failed_jobs --requeue  # put them all back on the queue

On EC2: docker compose exec worker python -m scripts.failed_jobs
"""

import argparse

from rq import Queue
from rq.job import Job
from rq.registry import FailedJobRegistry

from app.queue import queue as app_queue


def failed_jobs(queue: Queue) -> list[Job]:
    """The jobs RQ moved to its failed job registry, oldest first."""
    registry = FailedJobRegistry(queue=queue)
    return [job for job in Job.fetch_many(registry.get_job_ids(), queue.connection) if job]


def main(argv: list[str] | None = None, queue: Queue = app_queue) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requeue", action="store_true", help="run every failed job again")
    args = parser.parse_args(argv)
    jobs = failed_jobs(queue)
    for job in jobs:
        # The last line of the traceback is the error itself.
        error = (job.latest_result().exc_string or "").strip().splitlines()[-1:]
        print(f"{job.id} {job.func_name}{job.args} failed: {''.join(error)}")
        if args.requeue:
            FailedJobRegistry(queue=queue).requeue(job.id)
    print(f"{len(jobs)} failed jobs" + (", all queued again" if args.requeue and jobs else ""))


if __name__ == "__main__":
    main()
