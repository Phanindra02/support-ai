"""Each new ticket's priority comes from the model service, by way of a worker job."""

import httpx
import pytest

from app.config import settings
from app.models import Ticket, TicketPriority
from app.queue import PREDICT_PRIORITY, PREDICT_RETRY, get_queue, make_queue
from worker.jobs import set_predicted_priority

MODEL_SERVICE_URL = "http://model:8000"


@pytest.fixture
def model_service_url(monkeypatch):
    monkeypatch.setattr(settings, "model_service_url", MODEL_SERVICE_URL)


def queued(test_queue):
    return [(job.func_name, job.args) for job in test_queue.get_jobs()]


def fake_model_service(reply):
    """An HTTP client whose every request gets reply(request), and the requests it got."""
    requests = []

    def handle(request):
        requests.append(request)
        return reply(request)

    return httpx.Client(transport=httpx.MockTransport(handle)), requests


def stored_priority(session_factory, ticket_id):
    with session_factory() as db:
        return db.get(Ticket, ticket_id).priority


# Queueing the job


def test_a_new_ticket_queues_its_priority_prediction(make_ticket, test_queue, model_service_url):
    ticket = make_ticket()

    assert queued(test_queue) == [(PREDICT_PRIORITY, (ticket["id"],))]


def test_nothing_is_queued_until_the_model_service_is_set_up(make_ticket, test_queue, monkeypatch):
    monkeypatch.setattr(settings, "model_service_url", None)

    make_ticket()

    assert queued(test_queue) == []


def test_a_ticket_still_opens_at_medium_if_the_queue_cant_be_reached(
    test_app, make_ticket, model_service_url
):
    # Nothing listens on port 1.
    test_app.dependency_overrides[get_queue] = lambda: make_queue("redis://localhost:1/0")

    assert make_ticket()["priority"] == "medium"


def test_predictions_are_tried_3_more_times_soon_after():
    assert PREDICT_RETRY.max == 3
    assert PREDICT_RETRY.intervals == [5, 30, 120]


# The job


def test_the_job_saves_the_predicted_priority(make_ticket, session_factory, model_service_url):
    ticket = make_ticket(subject="Site down", message="Nobody can log in.")
    client, requests = fake_model_service(
        lambda request: httpx.Response(200, json={"label": "high"})
    )

    with session_factory() as db:
        assert set_predicted_priority(db, client, ticket["id"]) == TicketPriority.high

    assert stored_priority(session_factory, ticket["id"]) == TicketPriority.high
    [request] = requests
    assert str(request.url) == f"{MODEL_SERVICE_URL}/predict/priority"
    # The subject, a blank line, then the message, the way the training tickets read.
    assert request.read() == b'{"text":"Site down\\n\\nNobody can log in."}'


def test_if_the_model_service_fails_the_job_fails_and_the_ticket_keeps_its_priority(
    make_ticket, session_factory, model_service_url
):
    ticket = make_ticket()
    client, _ = fake_model_service(lambda request: httpx.Response(503))

    with session_factory() as db, pytest.raises(httpx.HTTPStatusError):
        set_predicted_priority(db, client, ticket["id"])

    assert stored_priority(session_factory, ticket["id"]) == TicketPriority.medium


def test_a_ticket_that_no_longer_exists_is_skipped(session_factory, model_service_url):
    client, requests = fake_model_service(lambda request: httpx.Response(200))

    with session_factory() as db:
        assert set_predicted_priority(db, client, 404) is None
    assert requests == []
