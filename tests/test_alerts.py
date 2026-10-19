"""Marking a ticket urgent posts it to n8n, which posts it to Slack. A fake n8n stands in."""

import json
import logging

import httpx
import pytest

from app.alerts import http_client
from app.config import settings

SECRET = "made-up-n8n-secret-for-tests"


@pytest.fixture(autouse=True)
def n8n_secret(monkeypatch):
    monkeypatch.setattr(settings, "n8n_webhook_secret", SECRET)


@pytest.fixture
def ticket(make_ticket):
    return make_ticket(customer_name="Jane Doe", subject="Card charged twice")


def mark(client, ticket, **changes):
    response = client.patch(f"/tickets/{ticket['id']}", json=changes)
    assert response.status_code == 200, response.text
    return response


def test_marking_a_ticket_urgent_posts_it_to_n8n(client, ticket, n8n):
    mark(client, ticket, priority="urgent")

    [request] = n8n.requests
    assert request.method == "POST"
    assert str(request.url) == settings.n8n_webhook_url
    assert json.loads(request.content) == {
        "ticket_id": ticket["id"],
        "subject": "Card charged twice",
        "customer_name": "Jane Doe",
        "status": "open",
        "priority": "urgent",
        "created_at": ticket["created_at"],
        "marked_urgent_by": "Ada Lovelace",  # The agent the client fixture logs in as.
    }


def test_the_alert_leaves_out_the_customers_phone_number(client, ticket, n8n):
    mark(client, ticket, priority="urgent")
    assert ticket["customer"]["phone"] not in n8n.requests[0].content.decode()


def test_marking_an_urgent_ticket_urgent_again_sends_nothing(client, ticket, n8n):
    mark(client, ticket, priority="urgent")
    mark(client, ticket, priority="urgent")
    mark(client, ticket, status="pending", priority="urgent")
    assert len(n8n.requests) == 1


@pytest.mark.parametrize(
    "changes", [{"priority": "high"}, {"priority": "low"}, {"status": "closed"}]
)
def test_other_changes_send_nothing(client, ticket, n8n, changes):
    mark(client, ticket, **changes)
    assert n8n.requests == []


def test_a_ticket_made_urgent_again_after_lowering_alerts_again(client, ticket, n8n):
    mark(client, ticket, priority="urgent")
    mark(client, ticket, priority="high")
    mark(client, ticket, priority="urgent")
    assert len(n8n.requests) == 2


def raise_connect_error(request):
    raise httpx.ConnectError("Connection refused", request=request)


@pytest.mark.parametrize(
    "reply",
    [raise_connect_error, lambda request: httpx.Response(500), lambda request: httpx.Response(403)],
    ids=["n8n is down", "n8n fails", "n8n refuses"],
)
def test_the_update_succeeds_even_if_the_alert_fails(client, ticket, n8n, caplog, reply):
    n8n.reply = reply
    with caplog.at_level(logging.WARNING, logger="app.alerts"):
        response = mark(client, ticket, priority="urgent")

    assert response.json()["priority"] == "urgent"
    assert client.get(f"/tickets/{ticket['id']}").json()["priority"] == "urgent"
    assert "Couldn't send the urgent-ticket alert to n8n" in caplog.text


def test_calls_to_n8n_give_up_after_5_seconds():
    assert http_client.timeout == httpx.Timeout(5)


def test_every_alert_carries_the_shared_secret(client, ticket, n8n):
    mark(client, ticket, priority="urgent")
    assert n8n.requests[0].headers["X-Webhook-Secret"] == SECRET


@pytest.mark.parametrize("secret", [None, ""])
def test_no_alert_is_sent_until_the_secret_is_set(client, ticket, n8n, caplog, monkeypatch, secret):
    monkeypatch.setattr(settings, "n8n_webhook_secret", secret)
    with caplog.at_level(logging.WARNING, logger="app.alerts"):
        mark(client, ticket, priority="urgent")
    assert n8n.requests == []
    assert "N8N_WEBHOOK_SECRET isn't set" in caplog.text
