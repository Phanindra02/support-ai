"""Each new ticket is triaged by a worker job: priority from the model service, and an alert
when the ticket is high priority or the customer very negative."""

import json

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.models import ModelName, Prediction, Ticket, TicketPriority
from app.queue import TRIAGE_RETRY, TRIAGE_TICKET, get_queue, make_queue
from worker.jobs import MODEL_SERVICE_ATTEMPTS, ask_model, triage

MODEL_SERVICE_URL = "http://model:8000"


@pytest.fixture(autouse=True)
def waits(monkeypatch):
    """The retry waits ask_model would have slept, recorded instead of slept."""
    slept = []
    monkeypatch.setattr("worker.jobs.time.sleep", slept.append)
    return slept


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

    assert queued(test_queue) == [(TRIAGE_TICKET, (ticket["id"],))]


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
    assert TRIAGE_RETRY.max == 3
    assert TRIAGE_RETRY.intervals == [5, 30, 120]


# The job


def prediction(label, **scores):
    return {"label": label, "scores": scores, "model": "", "version": 1}


CALM = prediction("neutral", negative=0.1, neutral=0.8, positive=0.1)


@pytest.fixture
def alerts(n8n, monkeypatch):
    """A client for the fake n8n, which keeps every alert sent to it."""
    monkeypatch.setattr(settings, "n8n_webhook_secret", "made-up-n8n-secret")
    return httpx.Client(transport=httpx.MockTransport(n8n.handle))


def model_service(priority, sentiment=CALM):
    """A fake model service that answers each endpoint with its prediction."""
    replies = {"/predict/priority": priority, "/predict/sentiment": sentiment}
    return fake_model_service(lambda request: httpx.Response(200, json=replies[request.url.path]))


def sent_alerts(n8n):
    return [json.loads(request.content) for request in n8n.requests]


def test_triage_saves_the_predicted_priority(
    make_ticket, session_factory, model_service_url, alerts, n8n
):
    ticket = make_ticket(subject="Site down", message="Nobody can log in.")
    models, requests = model_service(prediction("low", low=0.7, medium=0.2, high=0.1))

    with session_factory() as db:
        assert triage(db, models, alerts, ticket["id"]) == TicketPriority.low

    assert stored_priority(session_factory, ticket["id"]) == TicketPriority.low
    assert [(str(request.url), json.loads(request.content)) for request in requests] == [
        # The subject, a blank line, then the message, the way the training tickets read.
        (f"{MODEL_SERVICE_URL}/predict/priority", {"text": "Site down\n\nNobody can log in."}),
        # The mood is in the customer's words alone.
        (f"{MODEL_SERVICE_URL}/predict/sentiment", {"text": "Nobody can log in."}),
    ]
    assert n8n.requests == []


@pytest.mark.parametrize(
    "priority, sentiment, reason",
    [
        (
            prediction("high", low=0.08, medium=0.1, high=0.82),
            CALM,
            "the model: high priority (82% sure)",
        ),
        (
            prediction("low", low=0.6, medium=0.3, high=0.1),
            prediction("negative", negative=0.93, neutral=0.05, positive=0.02),
            "the model: a very negative customer (93% sure)",
        ),
        (
            prediction("high", low=0.08, medium=0.1, high=0.82),
            prediction("negative", negative=0.8, neutral=0.15, positive=0.05),
            "the model: high priority (82% sure) and a very negative customer (80% sure)",
        ),
    ],
)
def test_a_high_priority_ticket_or_a_very_negative_customer_alerts_the_team(
    make_ticket, session_factory, model_service_url, alerts, n8n, priority, sentiment, reason
):
    ticket = make_ticket()
    models, _ = model_service(priority, sentiment)

    with session_factory() as db:
        triage(db, models, alerts, ticket["id"])

    [alert] = sent_alerts(n8n)
    assert alert["ticket_id"] == ticket["id"]
    assert alert["priority"] == priority["label"]
    # The n8n workflow's Slack message reads "Marked urgent by <this>."
    assert alert["marked_urgent_by"] == reason


def test_both_predictions_are_saved_for_retraining(
    make_ticket, session_factory, model_service_url, alerts
):
    ticket = make_ticket()
    priority = prediction("low", low=0.7, medium=0.2, high=0.1)
    models, _ = model_service(priority)

    with session_factory() as db:
        triage(db, models, alerts, ticket["id"])

    with session_factory() as db:
        saved = db.scalars(select(Prediction).order_by(Prediction.id)).all()
        assert [
            (row.ticket_id, row.model, row.version, row.label, row.scores) for row in saved
        ] == [
            (ticket["id"], ModelName.priority, 1, "low", priority["scores"]),
            (ticket["id"], ModelName.sentiment, 1, "neutral", CALM["scores"]),
        ]


def test_a_negative_customer_below_the_bar_sends_no_alert(
    make_ticket, session_factory, model_service_url, alerts, n8n
):
    ticket = make_ticket()
    negative = prediction("negative", negative=0.79, neutral=0.2, positive=0.01)
    models, _ = model_service(prediction("medium", low=0.2, medium=0.7, high=0.1), negative)

    with session_factory() as db:
        triage(db, models, alerts, ticket["id"])

    assert n8n.requests == []


def test_if_the_model_service_fails_nothing_is_saved_or_sent(
    make_ticket, session_factory, model_service_url, alerts, n8n
):
    ticket = make_ticket()
    # The priority answers, then the sentiment fails, so the job fails before saving.
    replies = {"/predict/priority": httpx.Response(200, json=prediction("high", high=0.9))}
    models, _ = fake_model_service(
        lambda request: replies.get(request.url.path, httpx.Response(503))
    )

    with session_factory() as db, pytest.raises(httpx.HTTPStatusError):
        triage(db, models, alerts, ticket["id"])

    assert stored_priority(session_factory, ticket["id"]) == TicketPriority.medium
    assert n8n.requests == []


def replies_in_turn(*replies):
    """A reply function that answers with each reply in turn, raising any that are errors."""
    remaining = list(replies)

    def reply(request):
        next_reply = remaining.pop(0)
        if isinstance(next_reply, Exception):
            raise next_reply
        return next_reply

    return reply


OK = httpx.Response(200, json=prediction("low", low=0.9))


@pytest.mark.parametrize(
    "first_failure",
    [
        httpx.Response(503),
        httpx.Response(502),
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
    ],
)
def test_a_brief_model_service_failure_is_asked_again(model_service_url, waits, first_failure):
    models, requests = fake_model_service(replies_in_turn(first_failure, OK))

    assert ask_model(models, "priority", "Refund please")["label"] == "low"
    assert len(requests) == 2
    assert len(waits) == 1


def test_a_failure_that_wont_pass_is_not_asked_again(model_service_url, waits):
    models, requests = fake_model_service(lambda request: httpx.Response(422))

    with pytest.raises(httpx.HTTPStatusError):
        ask_model(models, "priority", "Refund please")
    assert len(requests) == 1
    assert waits == []


def test_the_model_service_is_asked_at_most_3_times_with_growing_random_waits(
    model_service_url, waits
):
    models, requests = fake_model_service(lambda request: httpx.Response(503))

    with pytest.raises(httpx.HTTPStatusError):
        ask_model(models, "priority", "Refund please")
    assert len(requests) == MODEL_SERVICE_ATTEMPTS == 3
    # Full jitter: random, below a cap that doubles.
    assert len(waits) == 2
    assert 0 <= waits[0] <= 0.5
    assert 0 <= waits[1] <= 1


def test_a_ticket_that_no_longer_exists_is_skipped(session_factory, model_service_url, alerts):
    models, requests = fake_model_service(lambda request: httpx.Response(200))

    with session_factory() as db:
        assert triage(db, models, alerts, 404) is None
    assert requests == []
