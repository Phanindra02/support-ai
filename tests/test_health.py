"""The health checks Kubernetes runs: the API's /health and the worker's worker.health."""

import pytest
from rq import Queue, Worker

from app.queue import make_queue
from worker import health

HOSTNAME = "support-ai-worker-7d9f8-abcde"


def test_the_api_says_it_is_healthy_without_a_login(anonymous_client):
    response = anonymous_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.fixture
def worker_on(test_queue: Queue):
    """Starts registering workers in the test Redis, as a running worker does, under a hostname."""
    workers = []

    def start(hostname: str) -> Worker:
        worker = Worker([test_queue], connection=test_queue.connection)
        worker.hostname = hostname
        worker.register_birth()
        workers.append(worker)
        return worker

    yield start
    for worker in workers:
        worker.register_death()


def test_a_worker_on_this_host_is_alive(test_queue, worker_on):
    worker_on(HOSTNAME)
    assert health.is_alive(test_queue.connection, HOSTNAME)


def test_no_worker_is_not_alive(test_queue):
    assert not health.is_alive(test_queue.connection, HOSTNAME)


def test_a_worker_on_another_host_does_not_count(test_queue, worker_on):
    # Another pod's worker doesn't make this one healthy.
    worker_on("support-ai-worker-7d9f8-fghij")
    assert not health.is_alive(test_queue.connection, HOSTNAME)


def test_a_worker_that_stopped_is_not_alive(test_queue, worker_on):
    worker_on(HOSTNAME).register_death()
    assert not health.is_alive(test_queue.connection, HOSTNAME)


def test_a_worker_whose_heartbeat_expired_is_not_alive(test_queue, worker_on):
    worker = worker_on(HOSTNAME)
    # What Redis does when the worker stops renewing its key.
    test_queue.connection.delete(worker.key)
    assert not health.is_alive(test_queue.connection, HOSTNAME)


def test_main_exits_0_for_a_live_worker(test_queue, worker_on, monkeypatch):
    monkeypatch.setattr(health, "queue", test_queue)
    monkeypatch.setattr(health.socket, "gethostname", lambda: HOSTNAME)
    worker_on(HOSTNAME)
    assert health.main() == 0


def test_main_exits_1_without_a_worker(test_queue, monkeypatch, capsys):
    monkeypatch.setattr(health, "queue", test_queue)
    monkeypatch.setattr(health.socket, "gethostname", lambda: HOSTNAME)
    assert health.main() == 1
    assert "No worker on this host" in capsys.readouterr().err


def test_main_exits_1_when_redis_is_down(monkeypatch, capsys):
    # Nothing listens on port 1.
    monkeypatch.setattr(health, "queue", make_queue("redis://127.0.0.1:1/0"))
    assert health.main() == 1
    assert "Can't reach Redis" in capsys.readouterr().err
