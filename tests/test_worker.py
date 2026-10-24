"""The worker runs the jobs on the queue in Redis, and keeps each one's result or error."""

import pytest
from redis import Redis
from rq import SimpleWorker, Worker

import worker.main
from app.config import settings
from app.queue import make_queue, queue


@pytest.fixture
def test_queue(test_redis_url, redis_client):
    """A queue on the tests' Redis database, which redis_client empties before and after."""
    return make_queue(test_redis_url)


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
    monkeypatch.setattr(Worker, "work", lambda self, **options: listened.extend(self.queues))
    worker.main.main()
    assert [q.name for q in listened] == [queue.name]
    assert listened[0].connection is queue.connection
