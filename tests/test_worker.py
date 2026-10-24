"""The worker runs the jobs on the queue in Redis, and keeps each one's result or error."""

from redis import Redis
from rq import Retry, SimpleWorker, Worker

import scripts.failed_jobs
import worker.main
from app.config import settings
from app.queue import EMBED_RETRY, queue


def run_queued_jobs(test_queue):
    # SimpleWorker runs each job itself instead of in a forked copy, which is all a test
    # needs. burst=True stops it once the queue is empty, instead of waiting for more.
    SimpleWorker([test_queue], connection=test_queue.connection).work(burst=True)


def test_the_worker_runs_a_queued_job_and_keeps_its_result(test_queue):
    # A job names its function by import path, so the worker can import it in its own process.
    job = test_queue.enqueue("operator.add", 2, 3)
    assert job.get_status() == "queued"
    run_queued_jobs(test_queue)
    assert job.get_status() == "finished"
    assert job.return_value() == 5


def test_a_failed_job_is_kept_with_its_error_for_inspecting(test_queue):
    job = test_queue.enqueue("operator.truediv", 1, 0)
    run_queued_jobs(test_queue)
    assert job.get_status() == "failed"
    assert job.id in test_queue.failed_job_registry.get_job_ids()
    assert "ZeroDivisionError" in job.latest_result().exc_string


def test_the_api_and_the_worker_share_one_queue_on_the_apps_redis():
    expected = Redis.from_url(settings.redis_url).connection_pool.connection_kwargs
    options = queue.connection.connection_pool.connection_kwargs
    assert (options["host"], options["port"], options["db"]) == (
        expected["host"],
        expected["port"],
        expected["db"],
    )
    # RQ stores jobs as pickled bytes, which a client that decodes responses would garble.
    assert not options.get("decode_responses")


def test_the_worker_listens_on_that_queue(monkeypatch):
    listened = []
    options = {}

    def work(self, **given):
        listened.extend(self.queues)
        options.update(given)

    monkeypatch.setattr(Worker, "work", work)
    worker.main.main()
    # Without the scheduler, a job waiting to be tried again would never run.
    assert options == {"with_scheduler": True}
    assert [q.name for q in listened] == [queue.name]
    assert listened[0].connection is queue.connection


def test_a_job_that_keeps_failing_is_tried_again_then_kept_in_the_failed_list(
    test_queue, capsys
):
    # No waits between tries here, so the burst worker runs them all at once.
    job = test_queue.enqueue("operator.truediv", 1, 0, retry=Retry(max=2))
    run_queued_jobs(test_queue)

    job.refresh()
    assert job.get_status() == "failed"
    assert job.retries_left == 0  # It was tried 3 times: once, then 2 more.

    scripts.failed_jobs.main([], queue=test_queue)
    out = capsys.readouterr().out
    assert f"{job.id} operator.truediv(1, 0) failed: ZeroDivisionError: division by zero" in out
    assert out.endswith("1 failed jobs\n")

    scripts.failed_jobs.main(["--requeue"], queue=test_queue)
    assert scripts.failed_jobs.failed_jobs(test_queue) == []
    assert test_queue.get_job_ids() == [job.id]


def test_embedding_jobs_are_tried_3_more_times_with_growing_waits():
    assert EMBED_RETRY.max == 3
    assert EMBED_RETRY.intervals == [30, 120, 600]
